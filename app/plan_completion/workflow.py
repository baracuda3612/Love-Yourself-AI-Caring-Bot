"""Bounded plan completion delivery. PostgreSQL is the only send authority."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import exists, func

from app.active_days import resolve_timezone
from app.db import AIPlan, AIPlanDay, AIPlanStep, PlanCompletion, User
from app.plan_completion.chronology import utc

SEND_TIMEOUT = timedelta(minutes=2)
UNCERTAIN_TTL = timedelta(minutes=10)


def blocked_successor(plan_id):
    """SQL predicate shared by schedule, send, due scan, and product expiry."""
    return exists().where(
        PlanCompletion.successor_plan_id == plan_id,
        PlanCompletion.state.notin_(['sent', 'released']),
    )


def successor_delivery_blocked(db, plan_id):
    return bool(db.query(blocked_successor(plan_id)).scalar())


def lock_obligation(db, user_id, plan_id):
    from app.lifecycle import _lock_user
    _lock_user(db, user_id, require_entitlement=False)
    source = db.query(AIPlan).filter(AIPlan.id == plan_id, AIPlan.user_id == user_id).first()
    if source is None or source.status != 'completed':
        return None
    return db.query(PlanCompletion).filter(PlanCompletion.plan_id == plan_id).populate_existing().with_for_update().first()


def refresh_report_time(db, user, successor_id):
    """A saved DAY change also updates a not-yet-attempted no-action report."""
    from app.plan_completion.chronology import report_due_at, next_workday_day_at, last_terminal_step
    row = db.query(PlanCompletion).filter(
        PlanCompletion.successor_plan_id == successor_id,
        PlanCompletion.state == 'pending', PlanCompletion.attempts == 0).populate_existing().first()
    if row is None:
        return False
    last = last_terminal_step(db, row.plan_id)
    if last is not None and last.step_status == 'expired':
        saved_day = (user.profile.daily_time_slots or {}).get('DAY')
        previous = utc(row.report_at)
        if previous.astimezone(resolve_timezone(user.timezone)).strftime('%H:%M') == saved_day:
            return True
        now = datetime.now(timezone.utc)
        due = report_due_at(user, last, now=now)
        if utc(due) <= now < previous:
            due = next_workday_day_at(user, now)
        row.report_at = due
        row.wake_at = max(now, utc(due))
    return True


def continuation_text(db, row, user, now):
    from app.lifecycle import refresh_completion_successor
    successor = db.get(AIPlan, row.successor_plan_id)
    if successor.status == 'abandoned':
        return 'Автоматичне продовження скасовано або замінено за твоїм запитом.'
    if successor.status == 'paused':
        return ('У цьому плані більше немає незавершених вправ. '
            'Автоматичне продовження залишається на паузі. '
            'Щоб отримувати нові вправи, зніми паузу.')
    if successor.status != 'active':
        return 'Налаштування наступного плану вже змінено.'
    first = refresh_completion_successor(db, user=user, plan=successor, report_at=now)
    local_first = utc(first).astimezone(resolve_timezone(user.timezone))
    return (
        f'Наступні {successor.total_days} робочих днів уже готові в тому самому ритмі.\n'
        f'Перша вправа прийде {local_first:%d.%m.%Y} о {local_first:%H:%M} ({local_first:%Z}).\n'
        'Щоб змінити формат, час, призупинити чи скасувати план — просто скажи.'
    )


@dataclass(frozen=True)
class ReportClaim:
    plan_id: int
    chat_id: int
    payload: str
    release: bool = False


def claim_report(db, user_id, plan_id, *, now=None, release_operation_id=None):
    now = now or datetime.now(timezone.utc)
    row = lock_obligation(db, user_id, plan_id)
    if row is None:
        return None
    release = release_operation_id is not None
    if release:
        from app.lifecycle import find_lifecycle_operation, record_lifecycle_operation
        if find_lifecycle_operation(db, user_id, release_operation_id):
            return None
        if row.state not in ('uncertain', 'release_uncertain', 'failed', 'sent_held'):
            return None
        last_start = row.release_started_at or row.started_at
        if last_start and now < utc(last_start) + UNCERTAIN_TTL:
            return None
    else:
        if row.state in ('in_flight', 'release_in_flight'):
            started = row.release_started_at if row.state == 'release_in_flight' else row.started_at
            if now >= utc(started) + SEND_TIMEOUT:
                row.state = 'release_uncertain' if row.state == 'release_in_flight' else 'uncertain'
                row.failure_code = 'interrupted_send'
            return None
        if row.state != 'pending' or now < utc(row.wake_at):
            return None
    user = db.get(User, user_id)
    from app.access import may_access
    if not may_access(db, user_id, delivery=True) or not user.tg_id:
        # Access pause/revocation parks the unfinished obligation. Scans exclude
        # inaccessible work; renewal/resume makes it eligible again.
        return None
    # Every attempted send first resolves current controls and future chronology.
    # No network call or receipt exists if generation fails: pending remains safe.
    continuation = continuation_text(db, row, user, now)
    if release:
        prefix = 'Підсумок доставлено із затримкою. Оновлюємо початок плану.' if row.sent_at else 'Доставку підсумку не підтверджено. Повторно його не надсилаємо.'
        payload = prefix + '\n\n' + continuation
        record_lifecycle_operation(db, user_id=user_id, plan_id=plan_id,
            source_operation_id=release_operation_id, operation='release_continuation', result_status='claimed')
        row.release_started_at = now
        row.release_payload = payload
        row.state = 'release_in_flight'
    else:
        from app.plan_completion.metrics import build_completion_metrics
        from app.plan_completion.report import build_completion_report
        from app.plan_completion.tokens import make_report_token
        from app.config import settings
        metrics = build_completion_metrics(db, user_id, plan_id)
        payload = build_completion_report(metrics, continuation=continuation)
        url = f'{settings.APP_BASE_URL}/report/{make_report_token(plan_id, settings.REPORT_TOKEN_SECRET)}'
        payload += f'\n\n🔗 <a href="{url}">Детальний звіт →</a>'
        row.payload = payload
        row.started_at = now
        row.attempts += 1
        row.state = 'in_flight'
    if row.chat_id is None:
        row.chat_id = user.tg_id
    elif row.chat_id != user.tg_id:
        raise ValueError('completion_chat_changed')
    row.wake_at = now + SEND_TIMEOUT
    db.flush()
    return ReportClaim(plan_id, user.tg_id, payload, release)


def finish_report(db, user_id, claim, *, outcome, message_id=None, retry_after=None, now=None):
    now = now or datetime.now(timezone.utc)
    row = lock_obligation(db, user_id, claim.plan_id)
    if row is None:
        return
    expected_payload = row.release_payload if claim.release else row.payload
    if expected_payload != claim.payload:
        return
    if row.state in ('sent', 'sent_held', 'released'):
        return
    if not claim.release and row.release_started_at is not None:
        if outcome == 'sent' and row.sent_at is None:
            row.sent_at, row.message_id = now, message_id
            db.flush()
        return
    if outcome == 'sent':
        if claim.release:
            row.state = 'released'
            row.released_at = now
            row.release_message_id = message_id
        else:
            row.state = 'sent'
            row.sent_at = now
            row.message_id = message_id
        # A response crossing local midnight can invalidate the promised seam.
        # Preserve the actual receipt, but require the same explicit recovery
        # confirmation instead of releasing work against an obsolete promise.
        successor = db.get(AIPlan, row.successor_plan_id)
        first = db.query(func.min(AIPlanStep.scheduled_for)).join(AIPlanDay).filter(
            AIPlanDay.plan_id == successor.id, AIPlanStep.step_status == 'pending').scalar()
        user = db.get(User, user_id)
        tz = resolve_timezone(user.timezone)
        late = successor.status == 'active' and first is not None and utc(first).astimezone(tz).date() <= now.astimezone(tz).date()
        if late:
            row.state = 'release_uncertain' if claim.release else 'sent_held'
        row.schedule_pending = not late
        row.wake_at = now
        row.failure_code = 'late_confirmation' if late else None
    elif claim.release:
        row.state = 'release_uncertain'
        row.failure_code = 'release_unconfirmed'
    elif outcome == 'retryable' and row.attempts < 3:
        row.state = 'pending'
        row.wake_at = now + timedelta(seconds=max(30, retry_after or 30))
        row.failure_code = 'rate_limit'
    else:
        row.state = 'uncertain' if outcome == 'uncertain' else 'failed'
        row.failure_code = outcome
    db.flush()
    if outcome == 'sent' and not claim.release:
        # Receipt durability does not depend on secondary telemetry availability.
        try:
            with db.begin_nested():
                from app.telemetry import write_event_operation
                from app.plan_completion.metrics import build_completion_metrics
                write_event_operation(db, user_id=user_id, event_name='plan_completion_sent',
                    event_source='runtime', source_operation_id=f'runtime:completion-report:{claim.plan_id}',
                    plan_id=claim.plan_id, occurred_at=now,
                    attribution_at=row.started_at,
                    properties={'outcome_tier': build_completion_metrics(db,user_id,claim.plan_id).outcome_tier})
        except Exception:
            import logging
            logging.getLogger(__name__).exception('Completion receipt saved; telemetry unavailable plan=%s',claim.plan_id)


async def deliver_report(user_id, plan_id, *, release_operation_id=None):
    """Claim/receipt work is off the polling loop; an ambiguous call never resends."""
    import asyncio
    from app.db import SessionLocal
    from app.lifecycle import CompletionDeliveryResult
    from app.telegram import bot
    from app.exercise_delivery import _DEFINITE_REJECTIONS
    from aiogram.exceptions import TelegramRetryAfter
    from app.scheduler import enable_completion_recovery

    def claim_transaction():
        with SessionLocal() as db:
            claim = claim_report(db, user_id, plan_id, release_operation_id=release_operation_id)
            db.commit()
            return claim
    try:
        claim = await asyncio.to_thread(claim_transaction)
    except Exception:
        def park_failed_preparation():
            with SessionLocal() as db:
                row = lock_obligation(db, user_id, plan_id)
                if row is not None and row.state == 'pending':
                    row.attempts += 1
                    row.failure_code = 'preparation_failed'
                    row.state = 'pending' if row.attempts < 3 else 'failed'
                    row.wake_at = datetime.now(timezone.utc) + timedelta(minutes=5)
                    db.commit()
        try:
            await asyncio.to_thread(park_failed_preparation)
        except Exception:
            pass  # An unavailable DB leaves the original obligation durable.
        await asyncio.to_thread(enable_completion_recovery)
        return CompletionDeliveryResult(user_id, plan_id, False, code='preparation_failed', retry_scheduled=True)
    if claim is None:
        await asyncio.to_thread(enable_completion_recovery)
        return CompletionDeliveryResult(user_id, plan_id, False, code='not_claimed')
    # Arm the interrupted-send deadline before crossing the external boundary.
    await asyncio.to_thread(enable_completion_recovery)
    outcome, message_id, retry_after, delivered_at = 'uncertain', None, None, None
    try:
        message = await asyncio.wait_for(bot.send_message(chat_id=claim.chat_id, text=claim.payload, parse_mode='HTML'), timeout=30)
        if type(message.message_id) is int and message.message_id > 0 and message.chat.id == claim.chat_id:
            outcome, message_id = 'sent', message.message_id
            delivered_at = getattr(message, 'date', None)
    except TelegramRetryAfter as exc:
        outcome, retry_after = 'retryable', exc.retry_after
    except _DEFINITE_REJECTIONS:
        outcome = 'failed'
    except Exception:
        pass  # Includes timeout/network loss: no proof of non-delivery.
    def receipt_transaction():
        with SessionLocal() as db:
            finish_report(db, user_id, claim, outcome=outcome, message_id=message_id, retry_after=retry_after, now=delivered_at)
            db.commit()
    try:
        await asyncio.to_thread(receipt_transaction)
    except Exception:
        # The committed in-flight claim remains the fence after process loss.
        # Keep the observed result for one bounded receipt-only local retry.
        try:
            await asyncio.to_thread(receipt_transaction)
        except Exception:
            await asyncio.to_thread(enable_completion_recovery)
            return CompletionDeliveryResult(user_id, plan_id, False, code='receipt_unconfirmed')
    await asyncio.to_thread(enable_completion_recovery)
    return CompletionDeliveryResult(user_id, plan_id, outcome == 'sent', code=outcome)

"""Bounded scheduled send journal. Unknown sends are never automatically retried."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.db import AIPlanStep, ExerciseDelivery, FeedbackEvent
from app.exercise_delivery import ExerciseSendResult
from app.exercise_presentation import ExerciseMedia, ExercisePresentation, step_presentation
from app.lifecycle import _lock_user, accept_confirmed_step_delivery
from app.telemetry import write_event_operation

LATE_GRACE = timedelta(hours=2)
MAX_ATTEMPTS = 3
IN_FLIGHT_GRACE = timedelta(minutes=2)


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def presentation_from_snapshot(payload):
    return ExercisePresentation(
        payload['exercise_id'], payload['content_version'], payload['title'],
        payload['duration_label'], tuple(payload['steps']),
        ExerciseMedia(**payload['media']) if payload['media'] else None,
        payload['status'], datetime.fromisoformat(payload['action_deadline']) if payload['action_deadline'] else None,
        tuple(payload['available_actions']),
    )


def latest_attempt(db, step_id):
    return db.query(ExerciseDelivery).filter(ExerciseDelivery.plan_step_id == step_id).order_by(ExerciseDelivery.attempt.desc()).first()


def sent_receipt(db, step_id):
    return db.query(ExerciseDelivery).filter(ExerciseDelivery.plan_step_id == step_id, ExerciseDelivery.state == 'delivered').first()


@dataclass(frozen=True)
class SendClaim:
    attempt_id: int
    chat_id: int
    presentation: ExercisePresentation


def claim_send(db, step_id, *, now=None):
    now = now or datetime.now(timezone.utc)
    step = db.get(AIPlanStep, step_id)
    if step is None:
        return None
    user_id = step.day.plan.user_id
    user = _lock_user(db, user_id, require_entitlement=False)
    db.refresh(step)
    plan = step.day.plan
    db.refresh(plan)
    prior = latest_attempt(db, step_id)
    if prior and prior.state != 'retryable':
        # A crash may leave an in-flight row. Its outcome cannot be inferred
        # from a lease timeout, so quarantine rather than resend.
        if prior.state == 'in_flight' and utc(prior.started_at) + IN_FLIGHT_GRACE <= now:
            prior.state = 'uncertain'
            prior.failure_code = 'interrupted_send'
        return None
    eligible = (
        user.is_active and plan.status == 'active' and step.step_status == 'pending'
        and step.scheduled_for and utc(step.scheduled_for) <= now
        and now <= utc(step.scheduled_for) + LATE_GRACE
        and step.expires_at and now < utc(step.expires_at)
    )
    if not eligible:
        if prior and (step.step_status != 'pending' or not step.scheduled_for or not step.expires_at or now > utc(step.scheduled_for) + LATE_GRACE or now >= utc(step.expires_at)):
            prior.state = 'terminal_failure'
            prior.failure_code = 'delivery_window_closed'
        return None
    if prior and (prior.attempt >= MAX_ATTEMPTS or (prior.next_attempt_at and now < utc(prior.next_attempt_at))):
        return None
    # Revalidate exact DB version and medical gate at each attempted send.
    from app.active_days import resolve_timezone
    try:
        presentation = step_presentation(db, step, user_timezone=resolve_timezone(user.timezone))
    except ValueError:
        failed = ExerciseDelivery(
            plan_step_id=step_id, source_operation_id=f'scheduler:delivery:{step_id}',
            attempt=prior.attempt + 1 if prior else 1, state='terminal_failure',
            chat_id=user.tg_id, presentation_snapshot={}, started_at=now, failure_code='invalid_presentation',
        )
        db.add(failed)
        db.flush()
        write_event_operation(db, user_id=user_id, event_name='task_delivery_failed', event_source='scheduler',
                              source_operation_id=f'{failed.source_operation_id}:attempt:{failed.attempt}',
                              plan_step_id=step_id, properties={'day_number': step.day.day_number, 'failure_code': failed.failure_code})
        return None
    if not presentation.available_actions:
        return None
    attempt = ExerciseDelivery(
        plan_step_id=step_id, source_operation_id=f'scheduler:delivery:{step_id}',
        attempt=prior.attempt + 1 if prior else 1, state='in_flight',
        chat_id=user.tg_id, presentation_snapshot=presentation.to_payload(), started_at=now,
    )
    db.add(attempt)
    db.flush()
    return SendClaim(attempt.id, attempt.chat_id, presentation)


def confirm_delivery(db, attempt, step, *, variant, payload, message_id, occurred_at):
    """Receipt, opportunity and lifecycle fact commit in the caller's transaction.

    A pause/cancel can win while Telegram is sending. Preserve the confirmed
    external fact without reopening a terminal step or inventing a send failure.
    """
    attempt.state = 'delivered'
    attempt.variant = variant
    attempt.rendered_payload = payload
    attempt.message_id = message_id
    attempt.confirmed_at = occurred_at
    attempt.failure_code = None
    attempt.next_attempt_at = None
    accept_confirmed_step_delivery(db, step_id=step.id, source_operation_id=attempt.source_operation_id, message_id=message_id)
    db.flush()
    write_event_operation(
        db, user_id=step.day.plan.user_id, event_name='task_delivered', event_source='scheduler',
        source_operation_id=attempt.source_operation_id, plan_step_id=step.id,
        occurred_at=occurred_at, properties={'day_number': step.day.day_number},
    )


def finish_send(db, attempt_id, result, *, now=None):
    now = now or datetime.now(timezone.utc)
    attempt = db.get(ExerciseDelivery, attempt_id)
    if attempt is None:
        return
    step = db.get(AIPlanStep, attempt.plan_step_id)
    _lock_user(db, step.day.plan.user_id, require_entitlement=False)
    db.refresh(attempt)
    db.refresh(step)
    if attempt.state not in {'in_flight', 'uncertain'}:
        return
    if result.delivered:
        if result.chat_id != attempt.chat_id or type(result.message_id) is not int or result.message_id <= 0:
            raise ValueError('invalid Telegram receipt identity')
        confirm_delivery(db, attempt, step, variant=result.variant, payload=result.rendered_payload,
                         message_id=result.message_id, occurred_at=now)
        return
    attempt.variant = result.variant
    attempt.rendered_payload = result.rendered_payload
    attempt.failure_code = result.failure_code or result.outcome
    if result.outcome == 'uncertain':
        attempt.state = 'uncertain'
        return
    retry_at = now + timedelta(seconds=max(30, result.retry_after or 0))
    retryable = (
        result.retry_after is not None and attempt.attempt < MAX_ATTEMPTS
        and retry_at <= utc(step.scheduled_for) + LATE_GRACE
        and retry_at < utc(step.expires_at) and step.step_status == 'pending'
    )
    attempt.state = 'retryable' if retryable else 'terminal_failure'
    attempt.next_attempt_at = retry_at if retryable else None
    write_event_operation(
        db, user_id=step.day.plan.user_id, event_name='task_delivery_failed', event_source='scheduler',
        source_operation_id=f'{attempt.source_operation_id}:attempt:{attempt.attempt}',
        plan_step_id=step.id, properties={'day_number': step.day.day_number, 'failure_code': attempt.failure_code},
    )


def reconcile_callback_receipt(db, step_id, message):
    """An owned Telegram callback can prove a previously unknown send.

    Called only after locking/resolving the actor. Require the private chat,
    exact rendered instructions and our callback markup before binding identity.
    """
    from app.ux.exercise_renderer import render_exercise
    attempt = latest_attempt(db, step_id)
    if attempt is None or attempt.state not in {'in_flight', 'uncertain'}:
        return
    if message is None or message.chat.id != attempt.chat_id or message.chat.type != 'private':
        return
    presentation = presentation_from_snapshot(attempt.presentation_snapshot)
    variant = 'gif' if getattr(message, 'animation', None) else 'text'
    payload = render_exercise(presentation, caption=variant == 'gif')
    from html import unescape
    import re
    observed = message.caption if variant == 'gif' else message.text
    expected = unescape(re.sub(r'</?b>', '', payload))
    if observed != expected or not message.reply_markup:
        return
    buttons = {button.callback_data for row in message.reply_markup.inline_keyboard for button in row}
    if not {f'task_complete:{step_id}', f'task_skip:{step_id}'} <= buttons:
        return
    step = db.get(AIPlanStep, step_id)
    confirm_delivery(db, attempt, step, variant=variant, payload=payload,
                     message_id=message.message_id, occurred_at=utc(message.date))


def efficacy(db, step_id):
    return db.query(FeedbackEvent).filter(FeedbackEvent.plan_step_id == step_id, FeedbackEvent.source == 'exercise_efficacy').first()

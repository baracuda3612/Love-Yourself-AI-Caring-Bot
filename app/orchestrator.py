import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import logging
import re

import pytz
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import (
    AIPlan,
    ChatHistory,
    SessionLocal,
    User,
    UserEvent,
    UserProfile,
)
from app.logging.router_logging import log_metric
from app.session_memory import SessionMemory
from app.ux.persona import get_persona
from app.workers.coach_agent import _build_idle_finished_context, coach_agent
from app.workers.mock_workers import mock_onboarding_agent
from app.telemetry import log_user_event
from app.lifecycle import (
    CompletionDeliveryResult,
    CurrentMode,
    LifecycleEntitlementError,
    LifecycleResult,
    complete_current_plan_if_ready,
    derive_current_mode,
    get_current_plan as get_authoritative_current_plan,
    require_lifecycle_entitlement,
)

session_memory = SessionMemory(limit=20)
logger = logging.getLogger(__name__)
_completion_delivery_locks: dict[tuple[int, int], tuple[asyncio.Lock, int]] = {}
_completion_known_sends: dict[tuple[int, int], str] = {}


@asynccontextmanager
async def _serialize_completion_delivery(user_id: int, plan_id: int):
    """Serialize one plan's delivery attempts within the single runtime owner."""
    key = (user_id, plan_id)
    lock, waiters = _completion_delivery_locks.get(key, (asyncio.Lock(), 0))
    _completion_delivery_locks[key] = (lock, waiters + 1)
    acquired = False
    try:
        await lock.acquire()
        acquired = True
        yield
    finally:
        if acquired:
            lock.release()
        current = _completion_delivery_locks.get(key)
        if current is not None and current[0] is lock:
            remaining = current[1] - 1
            if remaining:
                _completion_delivery_locks[key] = (lock, remaining)
            else:
                del _completion_delivery_locks[key]


def _auto_complete_plan_if_needed(
    db: Session,
    user: User,
    *,
    expected_plan_id: int | None = None,
) -> LifecycleResult | None:
    require_lifecycle_entitlement(db, user.id)
    plan = None
    if expected_plan_id is None:
        plan = get_authoritative_current_plan(db, user.id)
        if plan is None:
            return None
        expected_plan_id = plan.id
    result = complete_current_plan_if_ready(
        db,
        user_id=user.id,
        plan_id=expected_plan_id,
        source_operation_id=f"runtime:complete:{expected_plan_id}",
    )
    if result is None or result.duplicate:
        return result
    if plan is None:
        plan = (
            db.query(AIPlan)
            .filter(AIPlan.id == result.plan_id, AIPlan.user_id == user.id)
            .first()
        )
    if plan is None:
        raise RuntimeError("completed_plan_missing")

    completion_rate = None
    adaptation_count = 0
    metrics_error = False
    try:
        from app.plan_metrics import get_completion_rate

        completion_rate = get_completion_rate(db, user.id, result.plan_id)
    except Exception as e:
        metrics_error = True
        logger.warning(
            "[COMPLETION] metrics failed user=%s plan=%s: %s",
            user.id,
            result.plan_id,
            e,
        )

    try:
        log_user_event(
            db=db,
            user_id=user.id,
            event_type="plan_completed",
            event_source="runtime",
            source_operation_id=f"runtime:complete:{result.plan_id}",
            plan_id=result.plan_id,
            context={
                "total_days": plan.total_days,
                "focus": plan.focus,
                "load": plan.load,
                "duration": plan.duration,
                "completion_rate": round(completion_rate, 4) if completion_rate is not None else None,
                "adaptation_count": adaptation_count,
                "metrics_error": metrics_error,
            },
        )
    except Exception as e:
        logger.error("[COMPLETION] log event failed user=%s: %s", user.id, e)

    return result


async def send_plan_completion_message(
    user_id: int,
    plan_id: int,
) -> CompletionDeliveryResult:
    """
    Explicitly sends the completion report after a committed transition.
    Exempt from MAX_AUTO_MESSAGES_PER_DAY — this is a lifecycle event.
    """
    async with _serialize_completion_delivery(user_id, plan_id):
        outcome_tier = _completion_known_sends.get((user_id, plan_id))
        if outcome_tier is not None:
            return _persist_completion_delivery_receipt(
                user_id,
                plan_id,
                outcome_tier,
            )
        return await _send_plan_completion_message_once(user_id, plan_id)


def _persist_completion_delivery_receipt(
    user_id: int,
    plan_id: int,
    outcome_tier: str,
) -> CompletionDeliveryResult:
    try:
        with SessionLocal() as db:
            log_user_event(
                db,
                user_id=user_id,
                event_type="plan_completion_sent",
                event_source="runtime",
                source_operation_id=f"runtime:completion-report:{plan_id}",
                plan_id=plan_id,
                context={"outcome_tier": outcome_tier},
            )
            db.commit()
    except Exception:
        logger.exception(
            "[COMPLETION_MSG] delivery receipt failed user=%s plan=%s",
            user_id,
            plan_id,
        )
        _schedule_completion_receipt_retry(user_id, plan_id)
        return CompletionDeliveryResult(
            user_id=user_id,
            plan_id=plan_id,
            succeeded=False,
            retry_scheduled=True,
            code="delivery_receipt_failed",
        )
    _completion_known_sends.pop((user_id, plan_id), None)
    return CompletionDeliveryResult(
        user_id=user_id,
        plan_id=plan_id,
        succeeded=True,
    )


async def _send_plan_completion_message_once(
    user_id: int,
    plan_id: int,
) -> CompletionDeliveryResult:
    from app.plan_completion.metrics import build_completion_metrics
    from app.plan_completion.report import build_completion_report
    from app.plan_completion.tokens import make_report_token
    from app.scheduler import _send_message_async

    with SessionLocal() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user or not user.tg_id:
            logger.warning("[COMPLETION_MSG] user=%s not found or no tg_id", user_id)
            return CompletionDeliveryResult(
                user_id=user_id,
                plan_id=plan_id,
                succeeded=False,
                code="user_unavailable",
            )
        plan = (
            db.query(AIPlan)
            .filter(
                AIPlan.id == plan_id,
                AIPlan.user_id == user_id,
                AIPlan.status == "completed",
            )
            .first()
        )
        if plan is None:
            logger.warning(
                "[COMPLETION_MSG] completed plan not owned user=%s plan=%s",
                user_id,
                plan_id,
            )
            return CompletionDeliveryResult(
                user_id=user_id,
                plan_id=plan_id,
                succeeded=False,
                code="completed_plan_not_owned",
            )

        already_sent = db.query(UserEvent).filter(
            UserEvent.user_id == user_id,
            UserEvent.event_name == "plan_completion_sent",
            UserEvent.plan_id == plan_id,
        ).first()
        if already_sent:
            _completion_known_sends.pop((user_id, plan_id), None)
            logger.info("[COMPLETION_MSG] already sent for plan=%s", plan_id)
            return CompletionDeliveryResult(
                user_id=user_id,
                plan_id=plan_id,
                succeeded=True,
                duplicate=True,
                code="already_sent",
            )

        try:
            metrics = build_completion_metrics(db, user_id, plan_id)
        except Exception as e:
            logger.error(
                "[COMPLETION_MSG] metrics failed user=%s plan=%s: %s",
                user_id,
                plan_id,
                e,
            )
            _schedule_completion_retry(user_id, plan_id)
            return CompletionDeliveryResult(
                user_id=user_id,
                plan_id=plan_id,
                succeeded=False,
                retry_scheduled=True,
                code="metrics_failed",
            )

        persona = "empath"
        if user.profile:
            persona = get_persona(user.profile)

        report_text = build_completion_report(metrics, persona)
        report_url = (
            f"{settings.APP_BASE_URL}/report/"
            f"{make_report_token(plan_id, settings.REPORT_TOKEN_SECRET)}"
        )
        report_text = report_text + f"\n\n🔗 <a href=\"{report_url}\">Детальний звіт →</a>"
        tg_id = user.tg_id

    result = await _send_message_async(tg_id, report_text)

    if result:
        _completion_known_sends[(user_id, plan_id)] = metrics.outcome_tier
        return _persist_completion_delivery_receipt(
            user_id,
            plan_id,
            metrics.outcome_tier,
        )

    _schedule_completion_retry(user_id, plan_id)
    return CompletionDeliveryResult(
        user_id=user_id,
        plan_id=plan_id,
        succeeded=False,
        retry_scheduled=True,
        code="send_failed",
    )


def _schedule_completion_retry(user_id: int, plan_id: int) -> None:
    from app.scheduler import scheduler

    scheduler.add_job(
        "app.orchestrator:_retry_completion_message",
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(minutes=30),
        args=[user_id, plan_id],
        id=f"completion_retry_{plan_id}",
        replace_existing=True,
    )
    logger.info("[COMPLETION_MSG] retry scheduled user=%s plan=%s", user_id, plan_id)


def _schedule_completion_receipt_retry(user_id: int, plan_id: int) -> None:
    from app.scheduler import scheduler

    scheduler.add_job(
        "app.orchestrator:_retry_completion_receipt",
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(minutes=30),
        args=[user_id, plan_id],
        id=f"completion_receipt_retry_{plan_id}",
        replace_existing=True,
    )


def _retry_completion_receipt(user_id: int, plan_id: int) -> None:
    outcome_tier = _completion_known_sends.get((user_id, plan_id))
    if outcome_tier is None:
        logger.warning(
            "[COMPLETION_RECEIPT_RETRY] known send unavailable user=%s plan=%s",
            user_id,
            plan_id,
        )
        return
    _persist_completion_delivery_receipt(user_id, plan_id, outcome_tier)


def _retry_completion_message(user_id: int, plan_id: int) -> None:
    """APScheduler sync callback — submits async send to event loop."""
    from app.scheduler import _submit_coroutine

    future = _submit_coroutine(send_plan_completion_message(user_id, plan_id))
    if future:
        try:
            delivery = future.result(timeout=30)
            if not delivery.succeeded:
                logger.warning(
                    "[COMPLETION_RETRY] pending user=%s plan=%s code=%s",
                    user_id,
                    plan_id,
                    delivery.code,
                )
        except Exception as e:
            logger.error("[COMPLETION_RETRY] failed user=%s: %s", user_id, e)
            _submit_coroutine(_send_failure_notice(user_id))


async def _send_failure_notice(user_id: int) -> None:
    from app.scheduler import _send_message_async

    with SessionLocal() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if user and user.tg_id:
            await _send_message_async(
                user.tg_id,
                "⚠️ Не вдалось надіслати звіт про завершення плану. "
                "Зверніться в підтримку якщо це повторюється.",
            )


def _trigger_plan_completion(user_id: int, plan_id: int) -> None:
    """
    APScheduler sync callback.
    Calls _auto_complete_plan_if_needed and submits send_plan_completion_message.
    """
    from app.scheduler import _submit_coroutine

    with SessionLocal() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return
        completion = _auto_complete_plan_if_needed(
            db,
            user,
            expected_plan_id=plan_id,
        )
        try:
            db.commit()
        except Exception as e:
            db.rollback()
            logger.error("[COMPLETION_TRIGGER] db commit failed user=%s: %s", user_id, e)
            return

    if completion is None:
        return

    future = _submit_coroutine(
        send_plan_completion_message(user_id, completion.plan_id)
    )
    if future:
        try:
            delivery = future.result(timeout=30)
            if not delivery.succeeded:
                logger.warning(
                    "[COMPLETION_TRIGGER] report pending user=%s plan=%s code=%s",
                    user_id,
                    completion.plan_id,
                    delivery.code,
                )
        except Exception as e:
            logger.error("[COMPLETION_TRIGGER] send failed user=%s: %s", user_id, e)


async def _auto_complete_plan_if_needed_for_user_id(user_id: int) -> None:
    with SessionLocal() as db:
        user: Optional[User] = db.query(User).filter(User.id == user_id).first()
        if not user:
            return

        completion = _auto_complete_plan_if_needed(db, user)

        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            logger.error("[LIFECYCLE] Failed to auto-complete plan for user %s", user_id)
            return

    if completion is not None:
        delivery = await send_plan_completion_message(user_id, completion.plan_id)
        if not delivery.succeeded:
            logger.warning(
                "[COMPLETION] report pending user=%s plan=%s code=%s",
                user_id,
                completion.plan_id,
                delivery.code,
            )


def _safe_timezone(name: Optional[str]) -> pytz.BaseTzInfo:
    try:
        return pytz.timezone(name or "Europe/Kyiv")
    except pytz.UnknownTimeZoneError:
        return pytz.timezone("Europe/Kyiv")


async def get_stm_history(user_id: int) -> List[Dict[str, str]]:
    """Short-term memory with Redis primary and Postgres fallback."""

    history = await session_memory.get_recent_messages(user_id)
    if history:
        return [
            {"role": item.get("role"), "content": item.get("text")}
            for item in history
            if isinstance(item, dict)
        ]

    with SessionLocal() as db:
        rows = (
            db.query(ChatHistory.role, ChatHistory.text, ChatHistory.created_at)
            .filter(ChatHistory.user_id == user_id)
            .order_by(ChatHistory.created_at.desc())
            .limit(session_memory.limit)
            .all()
        )

    return [
        {"role": row.role, "content": row.text}
        for row in reversed(rows)
    ]


async def get_ltm_snapshot(user_id: int) -> Dict[str, Any]:
    """Long-term snapshot: поля профілю користувача."""
    with SessionLocal() as db:
        profile: Optional[UserProfile] = (
            db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
        )

        if profile:
            # Access all relationship data while the session is active
            return {
                "main_goal": profile.main_goal,
                "communication_style": profile.communication_style,
                "name_preference": profile.name_preference,
                "timezone": profile.user.timezone if profile.user else None,
            }

    return {}


async def get_temporal_context(user_id: int) -> Optional[str]:
    with SessionLocal() as db:
        user: Optional[User] = db.query(User).filter(User.id == user_id).first()

    if not user:
        return None

    tz = _safe_timezone(user.timezone)
    localized_now = datetime.now(tz)
    hour = localized_now.hour

    if 5 <= hour < 12:
        period = "Morning"
    elif 12 <= hour < 17:
        period = "Afternoon"
    elif 17 <= hour < 22:
        period = "Evening"
    else:
        period = "Night"

    return f"{localized_now.strftime('%A')}, {localized_now.strftime('%H:%M')} ({period})"


async def get_fsm_state(user_id: int) -> Optional[str]:
    """Compatibility name returning the single derived current mode."""
    with SessionLocal() as db:
        user: Optional[User] = db.query(User).filter(User.id == user_id).first()
        if not user:
            return None
        return derive_current_mode(db, user_id).value


async def build_user_context(user_id: int, message_text: str) -> Dict[str, Any]:
    stm_history = await get_stm_history(user_id)
    current_mode = await get_fsm_state(user_id)
    temporal_context = await get_temporal_context(user_id)
    pending_action = await session_memory.get_pending_action(user_id)

    plan_type = None
    evening_slot_collected = False
    latest_plan_status = None
    with SessionLocal() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if user is not None:
            evening_slot_collected = bool(
                user.profile and user.profile.evening_slot_collected
            )
            latest_plan = (
                db.query(AIPlan)
                .filter(AIPlan.user_id == user_id)
                .order_by(AIPlan.cycle_number.desc(), AIPlan.id.desc())
                .first()
            )
            if latest_plan is not None:
                latest_plan_status = str(latest_plan.status)
                plan_type = "MEDIUM" if int(latest_plan.total_days) == 14 else "SHORT"

    return {
        "message_text": message_text,
        "short_term_history": stm_history,
        "current_mode": current_mode,
        "temporal_context": temporal_context,
        "plan_type": plan_type,
        "evening_slot_collected": evening_slot_collected,
        "latest_plan_status": latest_plan_status,
        "pending_action": pending_action,
    }


# Tool name → callable map (allowlist).
# Only these tools may be invoked by Coach via tool_call signal.
_PLAN_TOOL_REGISTRY: Dict[str, Any] = {}


def _recover_retained_switch(
    user_id: int,
    source_operation_id: str,
    hhmm: str,
) -> dict:
    """Use the internal receipt-gated recovery entrance, not a Coach tool."""
    from app.plan_runtime.tools import recover_plan_format_switch

    return recover_plan_format_switch(
        user_id,
        "MEDIUM",
        hhmm,
        source_operation_id=source_operation_id,
    )


def _build_tool_registry() -> Dict[str, Any]:
    """Lazy-build the tool registry so imports stay at call time."""
    from app.plan_runtime.tools import (
        cancel_plan,
        change_day_time,
        change_evening_time,
        create_followup_plan,
        get_plan_status,
        pause_plan,
        record_evening_time,
        resume_plan,
        retry_plan_action,
        retry_switch_plan_format,
        switch_plan_format,
    )
    return {
        "create_followup_plan": lambda uid, args: create_followup_plan(
            uid,
            args["plan_type"],
            source_operation_id=args["_source_operation_id"],
        ),
        "record_evening_time":  lambda uid, args: record_evening_time(
            uid,
            args["hhmm"],
            context=args["_evening_collection_context"],
            pending_source_operation_id=args["_evening_collection_source_id"],
            source_operation_id=args["_source_operation_id"],
        ),
        "change_day_time":      lambda uid, args: change_day_time(
            uid,
            args["hhmm"],
            source_operation_id=args["_source_operation_id"],
        ),
        "change_evening_time":  lambda uid, args: change_evening_time(
            uid,
            args["hhmm"],
            source_operation_id=args["_source_operation_id"],
        ),
        "get_plan_status":      lambda uid, _args: get_plan_status(uid),
        "pause_plan":           lambda uid, args: pause_plan(
            uid, source_operation_id=args["_source_operation_id"]
        ),
        "resume_plan":          lambda uid, args: resume_plan(
            uid, source_operation_id=args["_source_operation_id"]
        ),
        "cancel_plan":          lambda uid, args: cancel_plan(
            uid, source_operation_id=args["_source_operation_id"]
        ),
        "switch_plan_format":   lambda uid, args: switch_plan_format(
            uid,
            args["plan_type"],
            source_operation_id=args["_source_operation_id"],
        ),
        "retry_switch_plan_format": lambda uid, args: retry_switch_plan_format(
            uid,
            switch_source_operation_id=args.get("switch_source_operation_id"),
        ),
        "retry_plan_action": lambda uid, args: retry_plan_action(
            uid,
            args["action"],
            args["original_source_operation_id"],
        ),
    }


# Deterministic reply templates — no second LLM call needed.
_TOOL_REPLY_TEMPLATES: Dict[str, str] = {
    "create_followup_plan": "✅ Новий план запущено. Завдання приходитимуть за розкладом.",
    "record_evening_time":  "✅ Вечірній час збережено.",
    "change_day_time":      "✅ Денний час змінено. Наступні завдання прийдуть у новий час.",
    "change_evening_time":  "✅ Вечірній час змінено.",
    "get_plan_status":      None,   # returns dynamic data — formatted below
    "pause_plan":           "⏸ План поставлено на паузу. Завдання не надходитимуть до відновлення.",
    "resume_plan":          "▶️ План відновлено. Майбутній розклад потребує узгодження.",
    "cancel_plan":          "🛑 Поточну серію вправ скасовано.",
    "switch_plan_format":   "✅ Формат змінено, новий розклад узгоджено.",
    "retry_switch_plan_format": "✅ Зміну формату перевірено, розклад узгоджено.",
}


def _format_plan_status(result: Dict[str, Any]) -> str:
    if not result.get("plan_active"):
        return "📋 Активних 7 або 14 днів зараз немає."

    status_label = (
        "доставка вправ призупинена"
        if result.get("state") == "ACTIVE_PAUSED"
        else "доставка вправ активна"
    )
    return (
        f"📋 Стан: {status_label}\n"
        f"День {result.get('current_day', 1)} з {result.get('days_total', 0)} · "
        f"залишилось {result.get('days_remaining', 0)}\n"
        f"Виконано вправ: {result.get('steps_completed', 0)} з "
        f"{result.get('steps_total', 0)} "
        f"({result.get('completion_rate', 0)}%)"
    )


def _humanize_tool_error(tool_name: str, raw: str) -> str:
    """Map raw ValueError messages from plan_runtime/tools.py to user-friendly Ukrainian."""
    r = raw.lower()
    if "invalid time format" in r:
        return "Схоже, час введено неправильно. Напиши у форматі HH:MM, наприклад 09:30."
    if "only allowed from idle_onboarded" in r:
        return "Ця дія зараз недоступна — схоже, план уже є або щось пішло не так."
    if "followup_requires_abandoned_plan" in r:
        return "Новий план можна запустити тільки після скасування попереднього."
    if "followup_activation_requires_no_active_plan" in r:
        return "Спочатку потрібно завершити або скасувати поточний план."
    if "recoverable_switch_superseded" in r:
        return "⚠️ Ця збережена дія вже застаріла; поточний стан плану змінився."
    if "recoverable_switch_ambiguous" in r:
        return (
            "⚠️ Є кілька запитів на зміну формату. "
            "Для точного повтору потрібен код попередньої зміни."
        )
    if "switch_schedule_pending" in r:
        return (
            "⚠️ Спочатку потрібно узгодити розклад після зміни формату. "
            "Повтори перевірку тієї самої зміни."
        )
    if "switch_recovery_receipt_mismatch" in r:
        return "⚠️ Цей код не належить зміні формату."
    if "retry_action_superseded" in r:
        return "⚠️ Цю дію вже витіснила новіша зміна плану; старий повтор не застосовано."
    if "retry_receipt_missing" in r or "retry_receipt_action_mismatch" in r:
        return "⚠️ Не знайдено саме цієї збереженої дії. Перевір її код і тип."
    if "retry_source_required" in r or "unsupported_retry_action" in r:
        return "⚠️ Для повтору потрібні тип дії та її початковий код."
    if "requested_plan_type_already_current" in r:
        return "Цей формат уже діє; обери інший формат для перемикання."
    if "evening_time_requires_medium_plan" in r:
        return "Вечірній час можна змінювати лише для 14-денного плану."
    if "saved_day_time_missing" in r or "saved_day_time_invalid" in r:
        return "Спочатку потрібно налаштувати коректний денний час."
    if "cancel_plan requires" in r:
        return "Скасувати можна тільки активний або призупинений план."
    if "pause_plan requires" in r:
        return "Поставити на паузу можна тільки активний план."
    if "resume_plan requires" in r:
        return "Відновити можна тільки призупинений план."
    if "user" in r and "not found" in r:
        return "⚠️ Не вдалось виконати дію. Спробуй ще раз."
    # fallback — hide raw internals
    logger.debug("[TOOL] unmapped ValueError tool=%s: %s", tool_name, raw)
    return "⚠️ Не вдалось виконати дію. Спробуй ще раз."


def _retry_reference_note(value: Any) -> str:
    source = str(value or "")
    if not re.fullmatch(r"[A-Za-z0-9:_-]{1,160}", source):
        return ""
    return f" Код дії: {source}."


def _switch_success_reply(result: Dict[str, Any]) -> str:
    if result.get("plan_status") == "paused":
        base = "✅ Формат змінено. Новий план лишається на паузі; вправи почнуть надходити після відновлення."
    else:
        base = "✅ Формат змінено, новий розклад узгоджено."
    if not result.get("keyboard_cleanup_pending") and not result.get(
        "activation_event_pending"
    ):
        return base
    notices = []
    if result.get("keyboard_cleanup_pending"):
        notices.append("не вдалося прибрати старі кнопки; натисни їх ще раз або повтори перевірку за кодом")
    if result.get("activation_event_pending"):
        notices.append("запис події активації очікує повтору")
    warning_base = (
        base
        if result.get("plan_status") == "paused"
        else "✅ Новий план і розклад готові."
    )
    return warning_base + " " + "; ".join(notices) + "." + _retry_reference_note(result.get("switch_source_operation_id"))


def _followup_success_reply(result: Dict[str, Any]) -> str:
    if result.get("activation_event_pending"):
        return (
            "✅ План і розклад готові; запис події активації очікує повтору."
            + _retry_reference_note(result.get("original_source_operation_id"))
        )
    return _TOOL_REPLY_TEMPLATES["create_followup_plan"]


async def _execute_plan_tool(user_id: int, tool_call: Dict[str, Any]) -> Optional[str]:
    """
    Execute an allowlisted plan_runtime tool and return a user-facing reply string.
    Returns None if the tool name is not in the allowlist (orchestrator continues normally).
    """
    tool_name = str(tool_call.get("name") or "")
    tool_args = tool_call.get("arguments") or {}
    if not isinstance(tool_args, dict):
        tool_args = {}
    else:
        tool_args = dict(tool_args)
    source_operation_id = tool_call.get("call_id") or tool_call.get("id")
    source_required_tools = {
        "create_followup_plan",
        "record_evening_time",
        "change_day_time",
        "change_evening_time",
        "pause_plan",
        "resume_plan",
        "cancel_plan",
        "switch_plan_format",
        "retry_switch_plan_format",
        "retry_plan_action",
    }
    if tool_name in source_required_tools and not source_operation_id:
        logger.error(
            "[TOOL] mutation tool=%s user=%s missing stable call id",
            tool_name,
            user_id,
        )
        return "⚠️ Не вдалось виконати дію. Спробуй ще раз."
    tool_args["_source_operation_id"] = str(source_operation_id or "")

    pending_action = None
    if tool_name == "record_evening_time":
        pending_action = await session_memory.get_pending_action(user_id)
        if str(pending_action).startswith("collect_evening_time_for_medium:"):
            tool_args["_evening_collection_context"] = "followup"
        elif str(pending_action).startswith("collect_evening_time_for_switch:"):
            tool_args["_evening_collection_context"] = "switch"
        else:
            return "⚠️ Вечірній час зараз не очікується. Спочатку обери 14 днів."
        tool_args["_evening_collection_source_id"] = str(pending_action).split(":", 1)[1]

    registry = _build_tool_registry()
    handler = registry.get(tool_name)
    if handler is None:
        logger.warning("[TOOL] Unknown tool_call name=%r for user=%s — skipping", tool_name, user_id)
        return None

    if (
        tool_name == "record_evening_time"
        and tool_args.get("_evening_collection_context") == "switch"
    ):
        retained_source = tool_args["_evening_collection_source_id"]
        try:
            recovery = _recover_retained_switch(
                user_id,
                retained_source,
                str(tool_args.get("hhmm") or ""),
            )
        except ValueError as exc:
            raw_error = str(exc)
            logger.warning(
                "[TOOL] retained switch recovery user=%s failed: %s",
                user_id,
                exc,
            )
            if "Invalid time format" in raw_error:
                return _humanize_tool_error(tool_name, raw_error)
            if raw_error == "switch_recovery_evening_time_mismatch":
                return (
                    "⚠️ Цей час не збігається з уже збереженим. Для "
                    "відновлення введи попередній вечірній час; змінити його "
                    "можна після узгодження розкладу."
                )
            await session_memory.clear_pending_action(user_id)
            return "⚠️ Попередній запит на зміну формату вже недійсний."
        except Exception as exc:
            logger.error(
                "[TOOL] retained switch recovery user=%s error: %s",
                user_id,
                exc,
                exc_info=True,
            )
            return "⚠️ Не вдалось перевірити розклад. Спробуй ще раз."
        if recovery.get("status") != "not_ready":
            log_metric(
                "plan_tool_executed",
                extra={"user_id": user_id, "tool": "recover_plan_format_switch"},
            )
            if recovery.get("status") == "ok":
                await session_memory.clear_pending_action(user_id)
                return _switch_success_reply(recovery)
            if (
                recovery.get("code") == "switch_reconciliation_failed"
                and recovery.get("persisted") is True
            ):
                return (
                    "⚠️ Новий 14-денний план уже збережено, але його "
                    "розклад ще не узгоджено. Повтори введення того "
                    "самого часу — новий план вдруге не створиться."
                    + _retry_reference_note(retained_source)
                )
            if recovery.get("code") == "switch_proof_record_failed":
                return (
                    "⚠️ Розклад узгоджено, але підтвердження ще не збережено. "
                    "Повтори введення того самого часу."
                    + _retry_reference_note(retained_source)
                )
            await session_memory.clear_pending_action(user_id)
            return "⚠️ Цей запит уже застарів; поточний стан плану змінився."

    try:
        result = handler(user_id, tool_args)
        log_metric("plan_tool_executed", extra={"user_id": user_id, "tool": tool_name})
    except ValueError as exc:
        logger.warning("[TOOL] tool=%s user=%s failed: %s", tool_name, user_id, exc)
        return _humanize_tool_error(tool_name, str(exc))
    except Exception as exc:
        logger.error("[TOOL] tool=%s user=%s error: %s", tool_name, user_id, exc, exc_info=True)
        return "⚠️ Не вдалось виконати дію. Спробуй ще раз."

    # Special case: get_plan_status returns a dict to format
    if tool_name == "get_plan_status":
        return _format_plan_status(result)

    if (
        tool_name == "retry_switch_plan_format"
        and isinstance(result, dict)
        and result.get("status") == "needs_evening_time"
    ):
        pending_source = result.get("pending_source_operation_id")
        if not pending_source:
            return "⚠️ Не вдалось відновити очікування вечірнього часу."
        pending_key = f"collect_evening_time_for_switch:{pending_source}"
        await session_memory.set_pending_action(user_id, pending_key)
        if await session_memory.get_pending_action(user_id) != pending_key:
            return "⚠️ Запит збережено, але очікування вечірнього часу недоступне."
        return (
            "О котрій зручно отримувати вечірній момент? "
            "Напиши час у форматі 20:30."
            + _retry_reference_note(pending_source)
        )

    if isinstance(result, dict) and result.get("status") == "error":
        code = result.get("code")
        retry_reference = (
            result.get("original_source_operation_id")
            or result.get("switch_source_operation_id")
            or source_operation_id
        )
        retry_note = _retry_reference_note(retry_reference)
        if code == "switch_proof_record_failed":
            return (
                "⚠️ Розклад узгоджено, але підтвердження ще не збережено. "
                "Повтори перевірку цієї зміни." + retry_note
            )
        if code == "retry_postproof_failed":
            return (
                "⚠️ Не вдалося остаточно перевірити стан цієї дії. "
                "Повтори перевірку за тим самим кодом." + retry_note
            )
        if code == "activation_reconciliation_failed":
            return (
                "⚠️ План збережено, але його розклад не вдалося повністю "
                "узгодити. Повтори запуск із тим самим запитом."
                + retry_note
            )
        if code == "schedule_reconciliation_failed":
            return (
                "⚠️ Час збережено, але розклад ще не узгоджено. "
                "Повтори цю саму дію."
            )
        if code == "superseded":
            if tool_name in {
                "change_day_time", "change_evening_time", "record_evening_time"
            }:
                authoritative_time = result.get("day_time") or result.get(
                    "evening_time"
                )
                if authoritative_time:
                    return (
                        "⚠️ Цей запит на зміну часу вже застарів. "
                        f"Актуальний час: {authoritative_time}."
                    )
            return "⚠️ Цей запит уже застарів; поточний стан плану змінився."
        if code == "cancel_reconciliation_failed":
            if result.get("historical_cleanup"):
                return (
                    "⚠️ Прибирання старого скасованого плану ще не завершене; "
                    "поточний план не змінено. Повтори перевірку."
                    + retry_note
                )
            return (
                "⚠️ План скасовано, але очищення розкладу ще не завершене. "
                "Повтори цю саму дію."
                + retry_note
            )
        if code in {
            "pause_reconciliation_failed",
            "resume_reconciliation_failed",
            "switch_reconciliation_failed",
        }:
            return (
                "⚠️ Зміну збережено, але розклад ще не узгоджено. "
                "Повтори цю саму дію."
                + retry_note
            )
        return "⚠️ Дію збережено частково. Повтори цей самий запит."

    # needs_evening_time soft result from create_followup_plan
    if isinstance(result, dict) and result.get("status") == "needs_evening_time":
        pending_kind = (
            "collect_evening_time_for_switch"
            if tool_name == "switch_plan_format"
            else "collect_evening_time_for_medium"
        )
        pending_key = f"{pending_kind}:{source_operation_id}"
        await session_memory.set_pending_action(user_id, pending_key)
        if await session_memory.get_pending_action(user_id) != pending_key:
            return (
                "⚠️ Запит збережено, але очікування вечірнього часу зараз "
                "недоступне. Повтори вибір 14-денного формату пізніше."
            )
        return (
            "О котрій зручно отримувати вечірній момент? "
            "Напиши час у форматі 20:30."
            + (
                _retry_reference_note(source_operation_id)
                if tool_name == "switch_plan_format"
                else ""
            )
        )

    # After record_evening_time: if pending_action is collect_evening_time_for_medium,
    # deterministically create the MEDIUM plan — no second LLM round-trip.
    if tool_name == "record_evening_time" and result.get("status") == "ok":
        pending = pending_action or await session_memory.get_pending_action(user_id)
        followup_prefix = "collect_evening_time_for_medium"
        switch_prefix = "collect_evening_time_for_switch"
        if (
            str(pending).startswith(f"{followup_prefix}:")
            or str(pending).startswith(f"{switch_prefix}:")
        ):
            activation_source_id = str(pending).split(":", 1)[1]
            registry = _build_tool_registry()
            try:
                cascade_tool = (
                    "switch_plan_format"
                    if str(pending).startswith(switch_prefix)
                    else "create_followup_plan"
                )
                activation = registry[cascade_tool](
                    user_id,
                    {
                        "plan_type": "MEDIUM",
                        "_source_operation_id": activation_source_id,
                    },
                )
                if not isinstance(activation, dict) or activation.get("status") != "ok":
                    if (
                        isinstance(activation, dict)
                        and activation.get("code") == "activation_reconciliation_failed"
                    ):
                        return (
                            "⚠️ План збережено, але його розклад не вдалося "
                            "повністю узгодити. Повтори введення часу."
                            + _retry_reference_note(activation_source_id)
                        )
                    if (
                        cascade_tool == "switch_plan_format"
                        and isinstance(activation, dict)
                        and activation.get("code") == "switch_reconciliation_failed"
                        and activation.get("persisted") is True
                    ):
                        return (
                            "⚠️ Новий 14-денний план уже збережено, але його "
                            "розклад ще не узгоджено. Повтори введення того "
                            "самого часу — новий план вдруге не створиться."
                            + _retry_reference_note(activation_source_id)
                        )
                    if (
                        cascade_tool == "switch_plan_format"
                        and isinstance(activation, dict)
                        and activation.get("code") == "switch_proof_record_failed"
                    ):
                        return (
                            "⚠️ Розклад узгоджено, але підтвердження ще не збережено. "
                            "Повтори введення того самого часу."
                            + _retry_reference_note(activation_source_id)
                        )
                    return (
                        "⚠️ Час збережено, але план не вдалось запустити. "
                        "Спробуй ще раз."
                        + _retry_reference_note(activation_source_id)
                    )
                log_metric("plan_tool_executed", extra={"user_id": user_id, "tool": cascade_tool})
                await session_memory.clear_pending_action(user_id)  # only after success
                return (
                    _switch_success_reply(activation)
                    if cascade_tool == "switch_plan_format"
                    else _followup_success_reply(activation)
                )
            except Exception as exc:
                logger.error("[TOOL] cascade create_followup_plan(MEDIUM) user=%s: %s", user_id, exc, exc_info=True)
                # pending_action preserved — user can retry
                return (
                    "⚠️ Час збережено, але план не вдалось запустити. Спробуй ще раз."
                    + _retry_reference_note(activation_source_id)
                )

    if tool_name == "cancel_plan":
        total_days = result.get("total_days")
        if total_days in {7, 14}:
            if result.get("keyboard_cleanup_pending"):
                return (
                    f"🛑 Поточні {total_days} днів скасовано. "
                    "Не вдалося прибрати старі кнопки; натисни їх ще раз або повтори перевірку за кодом."
                    + _retry_reference_note(result.get("original_source_operation_id"))
                )
            return f"🛑 Поточні {total_days} днів скасовано."

    if tool_name == "retry_plan_action":
        action = result.get("action")
        labels = {
            "pause": "Паузу",
            "resume": "Відновлення",
            "cancel": "Скасування",
            "followup": "Новий план",
        }
        if action == "cancel" and result.get("historical_cleanup"):
            reply = "✅ Прибирання старого скасованого плану перевірено; поточний план не змінено."
        elif action == "followup" and result.get("plan_status") == "paused":
            reply = (
                "✅ Новий план збережено й зараз на паузі. "
                "Активний розклад буде перевірено при відновленні."
            )
        else:
            reply = f"✅ {labels.get(action, 'Дію')} перевірено; розклад узгоджено."
        if result.get("keyboard_cleanup_pending"):
            reply += " Не вдалося прибрати старі кнопки; натисни їх ще раз або повтори перевірку за кодом."
        if result.get("activation_event_pending"):
            reply += " Запис події активації очікує повтору."
        if result.get("keyboard_cleanup_pending") or result.get("activation_event_pending"):
            reply += _retry_reference_note(result.get("original_source_operation_id"))
        return reply

    if tool_name in {"change_day_time", "change_evening_time"}:
        if result.get("jobs_reconciled") == "deferred":
            return (
                "✅ Час збережено для майбутнього розкладу. "
                "Розклад призупиненого плану зараз не змінено."
            )

    if tool_name in {"switch_plan_format", "retry_switch_plan_format"}:
        return _switch_success_reply(result)
    if (
        tool_name == "create_followup_plan"
        and result.get("recovered")
        and not result.get("activation_event_pending")
    ):
        return "✅ Збережений план знайдено, його розклад узгоджено."
    if tool_name == "create_followup_plan":
        return _followup_success_reply(result)

    if result.get("disposition") == "replayed":
        return "✅ Цю дію вже застосовано; актуальний стан підтверджено."

    template = _TOOL_REPLY_TEMPLATES.get(tool_name, "✅ Готово.")
    return template


async def handle_incoming_message(
    user_id: int,
    message_text: str,
) -> Dict[str, Any]:
    """
    Main orchestrator:
    - appends message to session memory
    - auto-completes plan if needed
    - builds user context from one derived current mode
    - routes ONBOARDING to the onboarding handler
    - else → calls coach_agent directly
    - executes allowlisted deterministic runtime tools
    - returns reply
    """

    await session_memory.append_message(user_id, "user", message_text)

    async def _finalize_reply(text: str) -> Dict[str, Any]:
        await session_memory.append_message(user_id, "assistant", text)
        return {"reply_text": text}

    try:
        await _auto_complete_plan_if_needed_for_user_id(user_id)
    except LifecycleEntitlementError:
        return await _finalize_reply("Доступ до Love Yourself зараз неактивний.")

    context_payload = await build_user_context(user_id, message_text)
    current_mode = context_payload.get("current_mode")

    if current_mode == CurrentMode.NO_ACTIVE_PLAN.value:
        with SessionLocal() as db:
            completion_context = _build_idle_finished_context(db, user_id)
        if completion_context is not None:
            context_payload["completion_context"] = completion_context

    if current_mode == CurrentMode.ONBOARDING.value:
        onboarding_payload = {
            "user_id": user_id,
            **context_payload,
            "message_text": message_text,
        }
        onboarding_result = await mock_onboarding_agent(onboarding_payload)
        return await _finalize_reply(str(onboarding_result.get("reply_text") or ""))

    # All live-user states → coach_agent directly
    coach_payload = {
        "user_id": user_id,
        **context_payload,
        "message_text": message_text,
    }
    worker_result = await coach_agent(coach_payload)

    reply_text = str(worker_result.get("reply_text") or "")
    error_payload = worker_result.get("error")
    if error_payload is not None:
        if error_payload.get("code") == "CONTRACT_MISMATCH":
            log_metric(
                "plan_contract_mismatch",
                extra={"user_id": user_id, "agent": "coach"},
            )
        log_metric(
            "plan_agent_error",
            extra={
                "timestamp": datetime.utcnow().isoformat(),
                "user_id": user_id,
                "agent": "coach",
                "error": error_payload,
            },
        )
        logger.warning(
            "[PLAN_AGENT] Error payload received for user %s (agent=coach): %s",
            user_id,
            error_payload,
        )
        return await _finalize_reply(reply_text)

    # ── Tool call execution (T5.8B) ───────────────────────────────────────────
    # Coach returns {"tool_call": {"name": "...", "arguments": {...}}}
    # Orchestrator executes allowlisted plan_runtime tools, then returns
    # a deterministic confirmation message — no second LLM round-trip needed.
    raw_tool_call = worker_result.get("tool_call")
    if raw_tool_call and isinstance(raw_tool_call, dict):
        tool_result = await _execute_plan_tool(user_id, raw_tool_call)
        if tool_result is not None:
            return await _finalize_reply(tool_result)

    return await _finalize_reply(reply_text)

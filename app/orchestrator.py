import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any, Dict, List, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.access import AccessDenied
from app.blocking_db import DBBusyError, run_db
from app.active_days import resolve_active_days, resolve_timezone
from app.config import settings
from app.db import (
    AIPlan,
    AIPlanDay,
    AIPlanStep,
    PlanLifecycleOperation,
    ChatHistory,
    SessionLocal,
    User,
    UserEvent,
    OnboardingProgress,
)
from app.logging.router_logging import log_metric
from app.session_memory import SessionMemory
from app.ux.persona import get_persona
from app.workers.assistant_agent import (
    _build_idle_finished_context,
    assistant_agent,
    assistant_tools,
    validated_tool_arguments,
)
from app.onboarding import FALLBACK as SETUP_FALLBACK
from app.telemetry import log_user_event
from app.plan_drafts.plan_builder_v5 import NoCandidatesError
from app.plan_schedule import read_next_reminder
from app.time_slots import resolve_daily_time_slots
from app.lifecycle import (
    CompletionDeliveryResult,
    CurrentMode,
    LifecycleEntitlementError,
    LifecycleResult,
    complete_current_plan_if_ready,
    derive_current_mode,
    derive_current_mode_from_facts,
    has_pending_medium_intent,
    is_completion_ready,
    read_lifecycle_status,
    get_current_plan as get_authoritative_current_plan,
    require_lifecycle_entitlement,
)

session_memory = SessionMemory(limit=20)
logger = logging.getLogger(__name__)
_ACCESS_DENIED_REPLY = "Доступ до Love Yourself зараз неактивний."
_NOTICE_REQUIRED_REPLY = (
    "Перед розмовою відкрий /privacy та підтвердь ознайомлення з актуальним повідомленням."
)
_SWITCH_UNAVAILABLE_REPLY = (
    "⚠️ Новий формат зараз недоступний. Поточний план не змінено. "
    "Спробуй змінити формат пізніше."
)


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

        with db.begin_nested():
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
        with db.begin_nested():
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


async def send_plan_completion_message(user_id: int, plan_id: int) -> CompletionDeliveryResult:
    from app.plan_completion.workflow import deliver_report
    return await deliver_report(user_id, plan_id)


def _trigger_plan_completion(user_id: int, plan_id: int) -> None:
    """Explicit post-commit entry; scheduler recovery uses the same source identity."""
    from app.scheduler import request_plan_completion
    request_plan_completion(user_id, plan_id)


async def _auto_complete_plan_if_needed_for_user_id(user_id: int) -> None:
    from app.plan_completion.continuation import prepare_automatic_continuation

    try:
        completion = await asyncio.to_thread(
            prepare_automatic_continuation, SessionLocal, user_id, _auto_complete_plan_if_needed,
        )
    except LifecycleEntitlementError:
        raise
    except Exception:
        logger.exception("Completion preparation pending user=%s", user_id)
        from app.scheduler import flag_completion_recovery
        recovery = flag_completion_recovery
    else:
        if completion is None:
            return
        from app.scheduler import enable_completion_recovery
        recovery = enable_completion_recovery
    try:
        await asyncio.to_thread(recovery)
    except Exception:
        logger.exception("Completion recovery wake failed user=%s", user_id)


async def get_stm_history(user_id: int) -> List[Dict[str, str]]:
    """Only retained durable conversation may enter the AI assistant context."""
    return [{"role":item['role'], "content":item['text']}
        for item in await session_memory.get_recent_messages(user_id)]


def _temporal_context(local_now: datetime) -> str:
    hour = local_now.hour
    if 5 <= hour < 12:
        period = "Morning"
    elif 12 <= hour < 17:
        period = "Afternoon"
    elif 17 <= hour < 22:
        period = "Evening"
    else:
        period = "Night"
    return local_now.strftime(f"%A, %H:%M ({period})")


def _assistant_plan_facts(db: Session, user: User) -> dict:
    """Read the current plan and runtime prerequisites while holding the user lock."""
    from app.time_slots import TimeSlotError, canonicalize_hhmm

    current = get_authoritative_current_plan(db, user.id)
    progress = db.get(OnboardingProgress, user.id) if current is None else None
    mode = derive_current_mode_from_facts(
        onboarding_stage=progress.stage if progress else None,
        onboarding_completed_at=progress.completed_at if progress else None,
        current_plan_status=current.status if current else None,
    )
    latest = current
    if mode is CurrentMode.NO_ACTIVE_PLAN:
        latest = (
            db.query(AIPlan).filter_by(user_id=user.id)
            .order_by(AIPlan.cycle_number.desc(), AIPlan.id.desc()).first()
        )
    # After abandonment, WP-02.3 still permits editing the saved preferences.
    runtime_plan = current or (latest if latest and latest.status == "abandoned" else None)
    profile = user.profile
    slots = profile.daily_time_slots if profile else None
    evening_time = None
    if profile and profile.evening_slot_collected and isinstance(slots, dict):
        try:
            evening_time = canonicalize_hhmm(slots.get("EVENING"))
        except TimeSlotError:
            pass
    plan_type = {7: "SHORT", 14: "MEDIUM"}.get(runtime_plan.total_days) if runtime_plan else None
    cycle_ending, can_retry_switch, retry_actions = (
        _assistant_current_plan_actions(db, current) if current else (False, False, ())
    )
    return {
        "cycle_ending": cycle_ending,
        "can_retry_switch": can_retry_switch,
        "retry_actions": retry_actions,
        "current_mode": mode.value,
        "current_plan_id": current.id if current else None,
        "runtime_plan_id": runtime_plan.id if runtime_plan else None,
        "runtime_plan_version": runtime_plan.version if runtime_plan else None,
        "plan_type": plan_type if current else None,
        "runtime_plan_type": plan_type,
        "latest_plan_status": latest.status if latest else None,
        "evening_slot_collected": bool(profile and profile.evening_slot_collected),
        "evening_time": evening_time,
    }


def _assistant_current_plan_actions(db: Session, plan: AIPlan) -> tuple[bool, bool, tuple[str, ...]]:
    receipts = db.query(PlanLifecycleOperation).filter_by(user_id=plan.user_id, plan_id=plan.id)
    previous_status = (
        db.query(AIPlan.status)
        .filter(AIPlan.user_id == plan.user_id, AIPlan.cycle_number < plan.cycle_number)
        .order_by(AIPlan.cycle_number.desc()).limit(1).scalar_subquery()
    )
    latest_control = (
        receipts.with_entities(PlanLifecycleOperation.operation)
        .filter(PlanLifecycleOperation.operation.in_(("pause", "resume")))
        .order_by(PlanLifecycleOperation.id.desc()).limit(1).scalar_subquery()
    )
    switch_activation = PlanLifecycleOperation.source_operation_id.startswith("switch-activate:")
    has_switch, has_switch_activation, has_activation, previous, control = db.query(
        receipts.filter_by(operation="switch_plan_format").exists(),
        receipts.filter_by(operation="activate").filter(switch_activation).exists(),
        receipts.filter_by(operation="activate").filter(~switch_activation).exists(),
        previous_status,
        latest_control,
    ).one()
    retry_actions = []
    if control == ("pause" if plan.status == "paused" else "resume"):
        retry_actions.append(control)
    if has_activation and previous == "abandoned":
        retry_actions.append("followup")
    return (
        is_completion_ready(db, plan),
        bool(has_switch or has_switch_activation),
        tuple(retry_actions),
    )


def _pending_medium_action(db: Session, user_id: int, facts: dict, pending: str | None) -> str | None:
    if not isinstance(pending, str) or facts["evening_slot_collected"]:
        return None
    prefix, _, source = pending.partition(":")
    if not re.fullmatch(r"[A-Za-z0-9:_-]{1,160}", source):
        return None
    if (
        prefix == "collect_evening_time_for_switch"
        and facts["current_plan_id"] is not None
        and facts["plan_type"] == "SHORT"
    ):
        context = "switch"
    elif (
        prefix == "collect_evening_time_for_medium"
        and facts["current_mode"] == CurrentMode.NO_ACTIVE_PLAN.value
        and facts["latest_plan_status"] == "abandoned"
    ):
        context = "followup"
    else:
        return None
    if has_pending_medium_intent(
        db, user_id=user_id, plan_id=facts["runtime_plan_id"],
        context=context, source_operation_id=source,
    ):
        return pending
    return None


def _assistant_access(db: Session, user_id: int) -> tuple[tuple[int, int], bool]:
    from app.privacy import read_pinned_notice

    return read_pinned_notice(db, user_id)


async def build_user_context(user_id: int, message_text: str) -> Dict[str, Any]:
    stm_history = await get_stm_history(user_id)
    pending_hint = await session_memory.get_pending_action(user_id)
    return await run_db(
        _build_user_context_db, user_id, message_text, stm_history, pending_hint,
    )


def _build_user_context_db(user_id, message_text, stm_history, pending_hint) -> Dict[str, Any]:
    with SessionLocal() as db:
        access, delivery_enabled = _assistant_access(db, user_id)
        user = db.get(User, user_id)
        facts = _assistant_plan_facts(db, user)
        pending = _pending_medium_action(
            db, user_id, facts, pending_hint,
        )
        timezone_name = user.timezone
        tz = resolve_timezone(timezone_name)
        now = datetime.now(timezone.utc)
        profile = user.profile
        context = {
            **facts,
            "message_text": message_text,
            "short_term_history": stm_history,
            "temporal_context": _temporal_context(now.astimezone(tz)),
            "access": access,
            "pending_action": pending,
            "current_day": None,
            "total_days": None,
            "days_remaining": None,
            "current_exercise_context": None,
            "name_preference": profile.name_preference if profile else None,
        }
        if facts["cycle_ending"]:
            from app.plan_completion.continuation import read_continuation_status

            context["continuation_state"] = read_continuation_status(db, facts["current_plan_id"], now)
        if facts["current_plan_id"] is not None and facts["plan_type"] is not None:
            from app.exercise_presentation import current_exercise_context

            status = read_lifecycle_status(db, user_id).to_runtime_payload()
            reminder_state, next_reminder = read_next_reminder(
                db, facts["current_plan_id"],
                paused=status["current_mode"] == CurrentMode.ACTIVE_PAUSED.value,
                timezone_name=timezone_name, delivery=delivery_enabled, now=now,
            )
            context.update(
                current_day=status["current_day"],
                total_days=status["days_total"],
                days_remaining=status["days_remaining"],
                steps_completed=status["steps_completed"],
                steps_total=status["steps_total"],
                schedule={
                    "day_time": resolve_daily_time_slots(profile)["DAY"],
                    "active_days": resolve_active_days(profile),
                    "timezone": timezone_name,
                    "reminder_state": reminder_state,
                    "next_reminder_at": next_reminder.isoformat(timespec="minutes") if next_reminder else None,
                },
                current_exercise_context=current_exercise_context(
                    db, user_id, facts["current_plan_id"], user_timezone=tz, now=now,
                ),
            )
        elif facts["current_mode"] == CurrentMode.NO_ACTIVE_PLAN.value:
            if (facts["latest_plan_status"] == "abandoned" and profile is not None
                    and isinstance(profile.daily_time_slots, dict)
                    and profile.daily_time_slots.get("DAY") is not None):
                context["schedule"] = {
                    "day_time": resolve_daily_time_slots(profile)["DAY"],
                    "active_days": resolve_active_days(profile),
                    "timezone": timezone_name,
                    "reminder_state": "no_active_plan",
                    "next_reminder_at": None,
                }
            context["completion_context"] = _build_idle_finished_context(db, user_id)
        from app.privacy_notice_cache import commit_read_check
        commit_read_check(db)
    return context


def _recover_retained_switch(
    user_id: int,
    source_operation_id: str,
    hhmm: str,
    **authorization,
) -> dict:
    """Use the internal receipt-gated recovery entrance, outside the AI assistant tool registry."""
    from app.plan_runtime.tools import recover_plan_format_switch

    return recover_plan_format_switch(
        user_id,
        "MEDIUM",
        hhmm,
        source_operation_id=source_operation_id,
        **authorization,
    )


def _build_tool_registry() -> Dict[str, Any]:
    """Lazy-build the tool registry so imports stay at call time."""
    from app.plan_runtime.tools import (
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
            **args.get("_authorization", {}),
        ),
        "record_evening_time":  lambda uid, args: record_evening_time(
            uid,
            args["hhmm"],
            context=args["_evening_collection_context"],
            pending_source_operation_id=args["_evening_collection_source_id"],
            source_operation_id=args["_source_operation_id"],
            **args.get("_authorization", {}),
        ),
        "change_day_time":      lambda uid, args: change_day_time(
            uid,
            args["hhmm"],
            source_operation_id=args["_source_operation_id"],
            **args.get("_authorization", {}),
        ),
        "change_evening_time":  lambda uid, args: change_evening_time(
            uid,
            args["hhmm"],
            source_operation_id=args["_source_operation_id"],
            **args.get("_authorization", {}),
        ),
        "get_plan_status":      lambda uid, args: get_plan_status(
            uid, **args.get("_authorization", {}),
        ),
        "pause_plan":           lambda uid, args: pause_plan(
            uid, source_operation_id=args["_source_operation_id"],
            **args.get("_authorization", {}),
        ),
        "resume_plan":          lambda uid, args: resume_plan(
            uid, source_operation_id=args["_source_operation_id"],
            **args.get("_authorization", {}),
        ),
        "switch_plan_format":   lambda uid, args: switch_plan_format(
            uid,
            args["plan_type"],
            source_operation_id=args["_source_operation_id"],
            **args.get("_authorization", {}),
        ),
        "retry_switch_plan_format": lambda uid, args: retry_switch_plan_format(
            uid,
            switch_source_operation_id=args.get("switch_source_operation_id"),
            **args.get("_authorization", {}),
        ),
        "retry_plan_action": lambda uid, args: retry_plan_action(
            uid,
            args["action"],
            args["original_source_operation_id"],
            **args.get("_authorization", {}),
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
    "switch_plan_format":   "✅ Формат змінено, новий розклад узгоджено.",
    "retry_switch_plan_format": "✅ Зміну формату перевірено, розклад узгоджено.",
}


def _format_plan_status(result: Dict[str, Any]) -> str:
    if not result.get("plan_active"):
        return "📋 Активних 7 або 14 днів зараз немає."

    if result.get("cycle_ending"):
        state = result.get("continuation_state")
        if state == "retry_wait":
            transition = "Підготовка наступного циклу не вдалася; система повторить її автоматично."
        elif state == "started":
            transition = "Підготовку наступного циклу розпочато; результат ще не підтверджено."
        elif state == "attempt_unconfirmed":
            transition = "Результат підготовки не підтверджено; система перевірить його й відновить спробу за потреби."
        else:
            transition = "Вправи цього циклу завершено; наступний цикл ще не підтверджено."
        return f"📋 {transition} Час наступної вправи поки не визначено фактичним розкладом."

    status_label = (
        "доставка вправ призупинена"
        if result.get("state") == "ACTIVE_PAUSED"
        else "доставка вправ вимкнена оператором"
        if result.get("delivery_enabled") is False
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


_STALE_ASSISTANT_ACTION_REPLY = "Обставини змінилися. Перевір поточний стан і повтори запит."
_INVALID_ASSISTANT_ACTION_REPLY = "Не вдалося виконати цю дію. Сформулюй запит ще раз."


class _AssistantContextChanged(ValueError):
    pass


async def _execute_assistant_tool(user_id: int, tool_call: dict, context: dict, offered_tools: list[dict]) -> str:
    """Authorize untrusted model output against this request and the live aggregate."""
    name = tool_call.get("name")
    offered = next((tool for tool in offered_tools if tool["name"] == name), None)
    if offered is None:
        return _INVALID_ASSISTANT_ACTION_REPLY
    try:
        arguments = validated_tool_arguments(offered, tool_call.get("arguments"))
    except ValueError:
        return _INVALID_ASSISTANT_ACTION_REPLY
    call_id = tool_call.get("call_id")
    if name != "get_plan_status" and (
        not isinstance(call_id, str) or not re.fullmatch(r"[A-Za-z0-9:_-]{1,160}", call_id)
    ):
        return _INVALID_ASSISTANT_ACTION_REPLY

    def recheck(db: Session, *, after_evening_save: bool = False) -> None:
        if name == "get_plan_status":
            access, _ = _assistant_access(db, user_id)
        else:
            # Mutations keep the actor and deployment locks through commit.
            from app.privacy import pinned_notice
            deployment, notice = pinned_notice(db, user_id, acknowledged=True)
            access = (deployment.id, notice.id)
        live = _assistant_plan_facts(db, db.get(User, user_id))
        identity = ("current_mode", "runtime_plan_id", "runtime_plan_version", "runtime_plan_type")
        if access != context["access"] or any(live[key] != context[key] for key in identity):
            raise _AssistantContextChanged()
        live["access"] = access
        if name == "record_evening_time":
            pending = session_memory.read_pending_action(user_id)
            if pending != context["pending_action"]:
                raise _AssistantContextChanged()
            if after_evening_save:
                if live["evening_time"] != arguments["hhmm"] or _pending_medium_action(
                    db, user_id, {**live, "evening_slot_collected": False}, pending,
                ) != pending:
                    raise _AssistantContextChanged()
                return
            live["pending_action"] = _pending_medium_action(db, user_id, live, pending)
        if name not in {tool["name"] for tool in assistant_tools(live)}:
            raise _AssistantContextChanged()
        if name == "retry_plan_action" and arguments["action"] not in live["retry_actions"]:
            raise _AssistantContextChanged()
        if name == "switch_plan_format" and arguments["plan_type"] == live["plan_type"]:
            raise ValueError("requested_plan_type_already_current")

    return await _execute_plan_tool(
        user_id, {"name": name, "arguments": arguments, "call_id": call_id},
        authorize=recheck,
        pending_action=context.get("pending_action"),
    ) or _INVALID_ASSISTANT_ACTION_REPLY


async def _execute_plan_tool(
    user_id: int, tool_call: Dict[str, Any], *, authorize=None, pending_action=None,
) -> Optional[str]:
    """
    Execute a backend-authorized runtime command and format its deterministic result.
    Model output must enter through _execute_assistant_tool.
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

    if authorize is not None:
        tool_args["_authorization"] = {"authorize": authorize}
    if tool_name == "record_evening_time":
        if authorize is None:
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

    from app.privacy import NoticeRequired

    if (
        tool_name == "record_evening_time"
        and tool_args.get("_evening_collection_context") == "switch"
    ):
        retained_source = tool_args["_evening_collection_source_id"]
        try:
            recovery = await asyncio.to_thread(
                _recover_retained_switch,
                user_id,
                retained_source,
                str(tool_args.get("hhmm") or ""),
                **tool_args.get("_authorization", {}),
            )
        except NoCandidatesError:
            logger.exception("[TOOL] retained switch generation unavailable user=%s", user_id)
            return _SWITCH_UNAVAILABLE_REPLY
        except _AssistantContextChanged:
            return _STALE_ASSISTANT_ACTION_REPLY
        except NoticeRequired:
            return _NOTICE_REQUIRED_REPLY
        except AccessDenied:
            return _ACCESS_DENIED_REPLY
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
        # Runtime handlers reconcile synchronous DB/scheduler/Telegram effects.
        # Keep their worker-owned edit loop outside the polling event loop.
        result = await asyncio.to_thread(handler, user_id, tool_args)
        log_metric("plan_tool_executed", extra={"user_id": user_id, "tool": tool_name})
    except _AssistantContextChanged:
        return _STALE_ASSISTANT_ACTION_REPLY
    except NoticeRequired:
        return _NOTICE_REQUIRED_REPLY
    except AccessDenied:
        return _ACCESS_DENIED_REPLY
    except ValueError as exc:
        logger.warning("[TOOL] tool=%s user=%s failed: %s", tool_name, user_id, exc)
        return _humanize_tool_error(tool_name, str(exc))
    except NoCandidatesError:
        logger.exception("[TOOL] plan generation unavailable tool=%s user=%s", tool_name, user_id)
        if tool_name in {"switch_plan_format", "retry_switch_plan_format"}:
            return _SWITCH_UNAVAILABLE_REPLY
        return "⚠️ Новий план зараз недоступний. Спробуй запустити його пізніше."
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
        if code in {"retry_postproof_failed", "postproof_failed"}:
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
                "Повтори цю саму дію." + retry_note
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
            cascade_tool = (
                "switch_plan_format"
                if str(pending).startswith(switch_prefix)
                else "create_followup_plan"
            )
            retry_instruction = (
                "Попроси перевірити зміну формату."
                if cascade_tool == "switch_plan_format"
                else "Попроси ще раз запустити 14-денний план."
            )
            try:
                activation_args = {
                    "plan_type": "MEDIUM",
                    "_source_operation_id": activation_source_id,
                }
                if authorize is not None:
                    activation_args["_authorization"] = {
                        "authorize": partial(authorize, after_evening_save=True),
                    }
                activation = await asyncio.to_thread(registry[cascade_tool], user_id, activation_args)
                if not isinstance(activation, dict) or activation.get("status") != "ok":
                    if (
                        isinstance(activation, dict)
                        and activation.get("code") == "activation_reconciliation_failed"
                    ):
                        return (
                            "⚠️ План збережено, але його розклад не вдалося "
                            "повністю узгодити. Попроси перевірити запуск плану."
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
                            "розклад ще не узгоджено. Попроси перевірити зміну формату."
                            + _retry_reference_note(activation_source_id)
                        )
                    if (
                        cascade_tool == "switch_plan_format"
                        and isinstance(activation, dict)
                        and activation.get("code") == "switch_proof_record_failed"
                    ):
                        return (
                            "⚠️ Розклад узгоджено, але підтвердження ще не збережено. "
                            "Попроси перевірити зміну формату."
                            + _retry_reference_note(activation_source_id)
                        )
                    return (
                        "⚠️ Час збережено, але план не вдалось запустити. "
                        + retry_instruction + _retry_reference_note(activation_source_id)
                    )
                log_metric("plan_tool_executed", extra={"user_id": user_id, "tool": cascade_tool})
                await session_memory.clear_pending_action(user_id)  # only after success
                return (
                    _switch_success_reply(activation)
                    if cascade_tool == "switch_plan_format"
                    else _followup_success_reply(activation)
                )
            except NoCandidatesError:
                logger.exception("[TOOL] cascade plan generation unavailable user=%s", user_id)
                if cascade_tool == "switch_plan_format":
                    return "Вечірній час збережено. " + _SWITCH_UNAVAILABLE_REPLY + _retry_reference_note(activation_source_id)
                return "⚠️ Вечірній час збережено. Попроси запустити 14-денний план пізніше."
            except Exception as exc:
                logger.error("[TOOL] cascade create_followup_plan(MEDIUM) user=%s: %s", user_id, exc, exc_info=True)
                # pending_action preserved — user can retry
                return (
                    "⚠️ Час збережено, але план не вдалось запустити. "
                    + retry_instruction + _retry_reference_note(activation_source_id)
                )

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
    - fences unfinished setup before conversation memory and the AI assistant
    - else → calls assistant_agent directly
    - executes allowlisted deterministic runtime tools
    - returns reply; the Telegram send boundary records delivered assistant memory
    """

    def _finalize_reply(text: str) -> Dict[str, Any]:
        return {"reply_text": text}

    from app.privacy import NoticeRequired, pinned_notice
    try:
        def check_access_and_setup():
            with SessionLocal() as db:
                pinned_notice(db, user_id, acknowledged=True)
                from app.privacy_notice_cache import commit_read_check
                is_setup = derive_current_mode(db, user_id) is CurrentMode.ONBOARDING
                commit_read_check(db)
                return is_setup

        if await run_db(check_access_and_setup):
            return _finalize_reply(SETUP_FALLBACK)
    except NoticeRequired:
        return _finalize_reply(_NOTICE_REQUIRED_REPLY)
    except AccessDenied:
        return _finalize_reply(_ACCESS_DENIED_REPLY)
    except DBBusyError:
        return _finalize_reply("Зараз багато запитів. Спробуй ще раз за кілька секунд.")
    try:
        await _auto_complete_plan_if_needed_for_user_id(user_id)
    except (AccessDenied, LifecycleEntitlementError):
        return _finalize_reply(_ACCESS_DENIED_REPLY)
    await session_memory.append_message(user_id, "user", message_text)

    try:
        context_payload = await build_user_context(user_id, message_text)
    except NoticeRequired:
        return _finalize_reply(_NOTICE_REQUIRED_REPLY)
    except (AccessDenied, LifecycleEntitlementError):
        return _finalize_reply(_ACCESS_DENIED_REPLY)
    except DBBusyError:
        return _finalize_reply("Зараз багато запитів. Спробуй ще раз за кілька секунд.")
    if context_payload.get("current_mode") == CurrentMode.ONBOARDING.value:
        return _finalize_reply(SETUP_FALLBACK)

    offered_tools = assistant_tools(context_payload)
    worker_result = await assistant_agent(context_payload, available_tools=offered_tools)

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
        return _finalize_reply(reply_text)

    raw_tool_call = worker_result.get("tool_call")
    if raw_tool_call is not None:
        if not isinstance(raw_tool_call, dict):
            return _finalize_reply(_INVALID_ASSISTANT_ACTION_REPLY)
        tool_result = await _execute_assistant_tool(user_id, raw_tool_call, context_payload, offered_tools)
        return _finalize_reply(tool_result)

    return _finalize_reply(reply_text)

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import logging

import pytz
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import (
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
    CurrentMode,
    complete_current_plan_if_ready,
    derive_current_mode,
    get_current_plan as get_authoritative_current_plan,
)

session_memory = SessionMemory(limit=20)
logger = logging.getLogger(__name__)


def _auto_complete_plan_if_needed(
    db: Session,
    user: User,
    *,
    expected_plan_id: int | None = None,
) -> int | None:
    plan = get_authoritative_current_plan(db, user.id)
    if plan is None or (
        expected_plan_id is not None and plan.id != expected_plan_id
    ):
        return None
    result = complete_current_plan_if_ready(
        db,
        user_id=user.id,
        plan_id=plan.id,
        source_operation_id=f"runtime:complete:{plan.id}",
    )
    if result is None or result.duplicate:
        return None

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

    return result.plan_id


async def send_plan_completion_message(user_id: int, plan_id: int) -> None:
    """
    Sends the completion report to the user via Telegram.
    Fire-and-forget. Called from _auto_complete_plan_if_needed.
    Exempt from MAX_AUTO_MESSAGES_PER_DAY — this is a lifecycle event.
    """
    from app.plan_completion.metrics import build_completion_metrics
    from app.plan_completion.report import build_completion_report
    from app.plan_completion.tokens import make_report_token
    from app.scheduler import _send_message_async

    with SessionLocal() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user or not user.tg_id:
            logger.warning("[COMPLETION_MSG] user=%s not found or no tg_id", user_id)
            return

        already_sent = db.query(UserEvent).filter(
            UserEvent.user_id == user_id,
            UserEvent.event_name == "plan_completion_sent",
            UserEvent.plan_id == plan_id,
        ).first()
        if already_sent:
            logger.info("[COMPLETION_MSG] already sent for plan=%s", plan_id)
            return

        try:
            metrics = build_completion_metrics(db, user_id, plan_id)
        except Exception as e:
            logger.error(
                "[COMPLETION_MSG] metrics failed user=%s plan=%s: %s",
                user_id,
                plan_id,
                e,
            )
            return

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
        with SessionLocal() as db:
            log_user_event(
                db,
                user_id=user_id,
                event_type="plan_completion_sent",
                event_source="runtime",
                source_operation_id=f"runtime:completion-report:{plan_id}",
                plan_id=plan_id,
                context={"outcome_tier": metrics.outcome_tier},
            )
            db.commit()
        return

    _schedule_completion_retry(user_id, plan_id)


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


def _retry_completion_message(user_id: int, plan_id: int) -> None:
    """APScheduler sync callback — submits async send to event loop."""
    from app.scheduler import _submit_coroutine

    future = _submit_coroutine(send_plan_completion_message(user_id, plan_id))
    if future:
        try:
            future.result(timeout=30)
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
        completed_plan_id = _auto_complete_plan_if_needed(
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

    if completed_plan_id is None:
        return

    future = _submit_coroutine(
        send_plan_completion_message(user_id, completed_plan_id)
    )
    if future:
        try:
            future.result(timeout=30)
        except Exception as e:
            logger.error("[COMPLETION_TRIGGER] send failed user=%s: %s", user_id, e)


def _auto_complete_plan_if_needed_for_user_id(user_id: int) -> None:
    with SessionLocal() as db:
        user: Optional[User] = db.query(User).filter(User.id == user_id).first()
        if not user:
            return

        completed_plan_id = _auto_complete_plan_if_needed(db, user)

        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            logger.error("[LIFECYCLE] Failed to auto-complete plan for user %s", user_id)
            return

    if completed_plan_id is not None:
        try:
            asyncio.get_running_loop()
            asyncio.create_task(send_plan_completion_message(user_id, completed_plan_id))
        except RuntimeError:
            logger.warning(
                "[COMPLETION] No running event loop, skipping message task user=%s",
                user_id,
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

    return {
        "message_text": message_text,
        "short_term_history": stm_history,
        "current_mode": current_mode,
        "temporal_context": temporal_context,
    }


# Tool name → callable map (allowlist).
# Only these tools may be invoked by Coach via tool_call signal.
_PLAN_TOOL_REGISTRY: Dict[str, Any] = {}


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
    )
    return {
        "create_followup_plan": lambda uid, args: create_followup_plan(
            uid,
            args.get("plan_type", "SHORT"),
            source_operation_id=args["_source_operation_id"],
        ),
        "record_evening_time":  lambda uid, args: record_evening_time(uid, args["hhmm"]),
        "change_day_time":      lambda uid, args: change_day_time(uid, args["hhmm"]),
        "change_evening_time":  lambda uid, args: change_evening_time(uid, args["hhmm"]),
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
    }


# Deterministic reply templates — no second LLM call needed.
_TOOL_REPLY_TEMPLATES: Dict[str, str] = {
    "create_followup_plan": "✅ Новий план запущено. Завдання приходитимуть за розкладом.",
    "record_evening_time":  "✅ Вечірній час збережено.",
    "change_day_time":      "✅ Денний час змінено. Наступні завдання прийдуть у новий час.",
    "change_evening_time":  "✅ Вечірній час змінено.",
    "get_plan_status":      None,   # returns dynamic data — formatted below
    "pause_plan":           "⏸ План поставлено на паузу. Завдання не надходитимуть до відновлення.",
    "resume_plan":          "▶️ План відновлено. Завдання повернуться за розкладом.",
    "cancel_plan":          "🛑 Поточну серію вправ скасовано.",
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
    if "only allowed from" in r and "followup" in r or "create_followup" in tool_name:
        return "Новий план можна запустити тільки після завершення поточного."
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
        "pause_plan",
        "resume_plan",
        "cancel_plan",
    }
    if tool_name in source_required_tools and not source_operation_id:
        logger.error(
            "[TOOL] mutation tool=%s user=%s missing stable call id",
            tool_name,
            user_id,
        )
        return "⚠️ Не вдалось виконати дію. Спробуй ще раз."
    tool_args["_source_operation_id"] = str(source_operation_id or "")

    registry = _build_tool_registry()
    handler = registry.get(tool_name)
    if handler is None:
        logger.warning("[TOOL] Unknown tool_call name=%r for user=%s — skipping", tool_name, user_id)
        return None

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

    # needs_evening_time soft result from create_followup_plan
    if isinstance(result, dict) and result.get("status") == "needs_evening_time":
        await session_memory.set_pending_action(user_id, "collect_evening_time_for_medium")
        return "О котрій зручно отримувати вечірній момент? Напиши час у форматі 20:30"

    # After record_evening_time: if pending_action is collect_evening_time_for_medium,
    # deterministically create the MEDIUM plan — no second LLM round-trip.
    if tool_name == "record_evening_time" and result.get("status") == "ok":
        pending = await session_memory.get_pending_action(user_id)
        if pending == "collect_evening_time_for_medium":
            registry = _build_tool_registry()
            try:
                registry["create_followup_plan"](
                    user_id,
                    {
                        "plan_type": "MEDIUM",
                        "_source_operation_id": (
                            f"{tool_args['_source_operation_id']}:followup"
                        ),
                    },
                )
                log_metric("plan_tool_executed", extra={"user_id": user_id, "tool": "create_followup_plan"})
                await session_memory.clear_pending_action(user_id)  # only after success
                return _TOOL_REPLY_TEMPLATES["create_followup_plan"]
            except Exception as exc:
                logger.error("[TOOL] cascade create_followup_plan(MEDIUM) user=%s: %s", user_id, exc, exc_info=True)
                # pending_action preserved — user can retry
                return "⚠️ Час збережено, але план не вдалось запустити. Спробуй ще раз."

    if tool_name == "cancel_plan":
        total_days = result.get("total_days")
        if total_days in {7, 14}:
            return f"🛑 Поточні {total_days} днів скасовано."

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

    _auto_complete_plan_if_needed_for_user_id(user_id)

    async def _finalize_reply(text: str) -> Dict[str, Any]:
        await session_memory.append_message(user_id, "assistant", text)
        return {"reply_text": text}

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

"""Telemetry helpers for logging events and managing engagement status."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import pytz
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from app.logic.rule_engine import RuleEngine

from app.db import (
    AIPlan,
    ContentLibrary,
    EngagementStatus,
    FailureSignal,
    PlanExecutionWindow,
    PlanInstance,
    TaskStats,
    User,
    UserEvent,
)

EDGE_OF_DAY_BUCKETS = {"night", "morning"}
TASK_EVENT_TYPES = {
    "task_delivered",
    "task_completed",
    "task_skipped",
    "task_ignored",
    "task_delayed",
}
ADAPTATION_EVENT_TYPES = {
    "adaptation_proposed",
    "adaptation_rejected",
    "adaptation_undo_requested",
    "adaptation_undo_blocked",
}
RESOURCE_EVENT_TYPES = {"task_viewed_resource"}
FRICTION_EVENT_TYPES = {"task_skipped", "task_ignored", "task_delayed", "task_failed"}
SKIP_STREAK_EVENT_TYPES = {"task_skipped", "task_ignored", "task_failed"}
SKIP_STREAK_RESET_EVENT_TYPES = {"task_completed"}
COMPLETION_EVENT_TYPES = {
    "task_completed",
    "task_skipped",
    "task_ignored",
    "task_delayed",
    "task_failed",
}

PROPOSAL_MESSAGES = {
    RuleEngine.PROPOSAL_REDUCE_LOAD: (
        "Ми помітили, що виконання зараз просідає.\n"
        "Хочеш зменшити навантаження або змінити план?"
    ),
    RuleEngine.PROPOSAL_OBSERVATION: (
        "Схоже, зараз важко тримати ритм.\n"
        "Хочеш змінити план або залишити як є?"
    ),
}

logger = logging.getLogger(__name__)


def _coerce_uuid(value: str | UUID | None) -> UUID | None:
    if value is None or isinstance(value, UUID):
        return value
    return UUID(str(value))


def _resolve_step_id(
    step_id: str | UUID | None,
    content_id: str | UUID | None,
    plan_step_id: str | int | UUID | None,
) -> str:
    if content_id is not None:
        return str(content_id)
    if step_id is not None:
        return str(step_id)
    if plan_step_id is not None:
        return str(plan_step_id)
    raise ValueError("Telemetry events require a step identifier.")


def _ensure_content_stub(db: Session, step_id: str, context: dict[str, Any]) -> None:
    if db.get(ContentLibrary, step_id):
        return
    title = context.get("plan_step_title") or context.get("title")
    description = context.get("plan_step_description") or context.get("description")
    payload = {
        "title": title,
        "description": description,
        "source": "plan_step_fallback",
    }
    db.add(
        ContentLibrary(
            id=step_id,
            content_version=1,
            internal_name=title or f"plan_step_{step_id}",
            category="plan_step",
            difficulty=1,
            energy_cost="LOW",
            logic_tags={},
            content_payload=payload,
            is_active=False,
        )
    )


async def _send_system_message_async(chat_id: int, text: str) -> None:
    from app.telegram import bot as tg_bot

    try:
        logger.info("[ADAPTATION_PROMPT] Sending system prompt to chat_id=%s", chat_id)
        await tg_bot.send_message(chat_id, text)
        logger.info("[ADAPTATION_PROMPT] System prompt sent to chat_id=%s", chat_id)
    except Exception as exc:  # pragma: no cover - safety net for runtime failures
        logger.error("[ADAPTATION_PROMPT] Failed to send system prompt to %s: %s", chat_id, exc)


def _dispatch_system_message(user: User, text: str) -> None:
    if not user.tg_id:
        logger.warning("[ADAPTATION_PROMPT] Skip dispatch: user_id=%s has no tg_id", user.id)
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        logger.info(
            "[ADAPTATION_PROMPT] Dispatch via running loop for user_id=%s chat_id=%s",
            user.id,
            user.tg_id,
        )
        loop.create_task(_send_system_message_async(user.tg_id, text))
        return

    try:
        logger.info(
            "[ADAPTATION_PROMPT] Dispatch via asyncio.run for user_id=%s chat_id=%s",
            user.id,
            user.tg_id,
        )
        asyncio.run(_send_system_message_async(user.tg_id, text))
    except RuntimeError as exc:
        logger.error("[ADAPTATION_PROMPT] Failed to dispatch system prompt for user %s: %s", user.id, exc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_timezone(user: User) -> pytz.BaseTzInfo:
    tz_name = user.timezone or "UTC"
    try:
        return pytz.timezone(tz_name)
    except pytz.UnknownTimeZoneError:
        return pytz.UTC


def _time_bucket(local_dt: datetime) -> str:
    hour = local_dt.hour
    if hour >= 23 or hour < 6:
        return "night"
    if 6 <= hour < 12:
        return "morning"
    if 12 <= hour < 18:
        return "day"
    return "evening"


def _ensure_plan_instance(
    db: Session,
    user_id: int,
    plan_instance_id: str | UUID | None = None,
    blueprint_id: str | None = None,
    initial_parameters: dict[str, Any] | None = None,
    contract_version: str | None = None,
    schema_version: str | None = None,
) -> PlanInstance:
    if plan_instance_id:
        instance_id = _coerce_uuid(plan_instance_id)
        instance = db.get(PlanInstance, instance_id)
        if instance:
            if instance.user_id != user_id:
                raise ValueError("Security Violation: Instance does not belong to user")
            return instance

    instance = (
        db.query(PlanInstance)
        .filter(PlanInstance.user_id == user_id)
        .order_by(desc(PlanInstance.created_at))
        .first()
    )
    if instance:
        return instance

    instance = PlanInstance(
        user_id=user_id,
        blueprint_id=blueprint_id,
        initial_parameters=initial_parameters or {},
        contract_version=contract_version or "v1",
        schema_version=schema_version or "v1",
    )
    db.add(instance)
    db.flush()
    return instance


def _ensure_execution_window(
    db: Session,
    instance: PlanInstance,
    server_now: datetime,
) -> PlanExecutionWindow:
    window = (
        db.query(PlanExecutionWindow)
        .filter(
            PlanExecutionWindow.instance_id == instance.id,
            PlanExecutionWindow.end_date.is_(None),
        )
        .order_by(desc(PlanExecutionWindow.start_date))
        .first()
    )
    if window:
        return window

    window = PlanExecutionWindow(
        instance_id=instance.id,
        start_date=server_now,
        engagement_status=EngagementStatus.ACTIVE,
    )
    db.add(window)
    db.flush()
    return window


def _record_plan_resumed_event(
    db: Session,
    user_id: int,
    window: PlanExecutionWindow,
    bucket: str,
    context: dict[str, Any],
    server_now: datetime,
) -> None:
    db.add(
        UserEvent(
            event_type="plan_resumed",
            timestamp=server_now,
            user_id=user_id,
            plan_execution_id=window.id,
            time_of_day_bucket=bucket,
            context=context,
        )
    )


def _get_or_create_task_stats(db: Session, user_id: int, step_id: str) -> TaskStats:
    stats = db.get(TaskStats, {"user_id": user_id, "step_id": step_id})
    if stats:
        return stats
    stats = TaskStats(
        user_id=user_id,
        step_id=step_id,
        attempts_total=0,
        completed_total=0,
        skipped_total=0,
        avg_reaction_sec=0.0,
        completed_edge_of_day=0,
    )
    db.add(stats)
    return stats


def _update_task_stats(
    stats: TaskStats,
    event_type: str,
    bucket: str,
    context: dict[str, Any],
) -> None:
    if event_type == "task_delivered":
        stats.attempts_total += 1
        return

    if event_type == "task_completed":
        stats.completed_total = (stats.completed_total or 0) + 1
        if bucket in EDGE_OF_DAY_BUCKETS:
            stats.completed_edge_of_day += 1
        reaction = context.get("reaction_sec")
        if reaction is not None:
            try:
                reaction_value = float(reaction)
            except (TypeError, ValueError):
                reaction_value = None
            if reaction_value is not None and stats.completed_total > 0:
                previous_total = stats.completed_total - 1
                stats.avg_reaction_sec = (
                    (stats.avg_reaction_sec * previous_total + reaction_value) / stats.completed_total
                )
        return

    if event_type == "task_skipped":
        stats.skipped_total += 1
        return

    if event_type == "task_delayed":
        return


def _maybe_create_failure_signal(
    db: Session,
    user_id: int,
    window: PlanExecutionWindow,
    step_id: str,
    event_type: str,
    context: dict[str, Any],
    server_now: datetime,
) -> None:
    if event_type not in {"task_skipped", "task_ignored", "task_delayed"}:
        return
    if event_type == "task_delayed":
        trigger_event = "delay"
    else:
        trigger_event = "ignore" if event_type == "task_ignored" else "skip"
    failure_context = context.get("failure_context_tag") or context.get("skip_reason")
    db.add(
        FailureSignal(
            user_id=user_id,
            plan_execution_id=window.id,
            step_id=step_id,
            trigger_event=trigger_event,
            failure_context_tag=failure_context,
            detected_at=server_now,
        )
    )


def _maybe_emit_system_prompt(
    db: Session,
    user: User,
    window: PlanExecutionWindow,
    event_type: str,
) -> None:
    logger.info(
        "[ADAPTATION_PROMPT] Evaluate trigger user_id=%s event_type=%s state=%s window_id=%s",
        user.id,
        event_type,
        user.current_state,
        window.id,
    )
    if event_type not in {"task_skipped", "task_ignored", "task_failed"}:
        logger.info("[ADAPTATION_PROMPT] Exit: unsupported event_type=%s", event_type)
        return
    if user.current_state != "ACTIVE":
        logger.info("[ADAPTATION_PROMPT] Exit: user_id=%s state=%s", user.id, user.current_state)
        return

    active_plan = (
        db.query(AIPlan)
        .filter(AIPlan.user_id == user.id, AIPlan.status == "active")
        .order_by(AIPlan.id.desc())
        .first()
    )
    if active_plan is None:
        logger.info("[ADAPTATION_PROMPT] Exit: user_id=%s has no active plan", user.id)
        return

    if not active_plan.load:
        raise RuntimeError("Invariant violation: active plan without load")

    normalized_load = active_plan.load.strip().upper()
    if normalized_load == "LITE":
        logger.info("[ADAPTATION_PROMPT] Exit: user_id=%s load=LITE", user.id)
        return

    threshold = RuleEngine.LOAD_THRESHOLDS.get(normalized_load)
    if threshold is None:
        logger.info("[ADAPTATION_PROMPT] Exit: unsupported load=%s", normalized_load)
        return

    skip_streak = get_skip_streak(db, user.id, RuleEngine.MAX_SKIP_THRESHOLD)
    logger.info(
        "[ADAPTATION_PROMPT] user_id=%s load=%s skip_streak=%s threshold=%s",
        user.id,
        normalized_load,
        skip_streak,
        threshold,
    )
    if skip_streak != threshold:
        logger.info("[ADAPTATION_PROMPT] Exit: skip_streak(%s) != threshold(%s)", skip_streak, threshold)
        return

    proposal = RuleEngine().evaluate(
        load=normalized_load,
        skip_streak=skip_streak,
    )
    if not proposal:
        logger.info("[ADAPTATION_PROMPT] Exit: no proposal from rule engine")
        return
    logger.info("[ADAPTATION_PROMPT] Proposal=%s for user_id=%s", proposal, user.id)

    message = PROPOSAL_MESSAGES.get(proposal)
    if not message:
        logger.warning("[ADAPTATION_PROMPT] Exit: message template missing for proposal=%s", proposal)
        return

    logger.info("[ADAPTATION_PROMPT] Dispatching system prompt for user_id=%s", user.id)
    _dispatch_system_message(user, message)
    try:
        log_user_event(
            db=db,
            user_id=user.id,
            event_type="adaptation_proposed",
            context={
                "proposal_type": proposal,
                "plan_id": str(active_plan.id),
                "plan_load": normalized_load,
                "skip_streak": skip_streak,
                "plan_execution_window_id": str(window.id),
            },
        )
    except Exception:
        logger.error(
            "[ADAPTATION_PROMPT] Failed to log adaptation_proposed for user_id=%s",
            user.id,
            exc_info=True,
        )


def _maybe_increment_batch_completion(
    db: Session,
    window: PlanExecutionWindow,
    server_now: datetime,
) -> None:
    recent_completed = (
        db.query(UserEvent)
        .filter(
            UserEvent.plan_execution_id == window.id,
            UserEvent.event_type == "task_completed",
        )
        .order_by(desc(UserEvent.timestamp))
        .limit(2)
        .all()
    )
    if len(recent_completed) < 2:
        return

    oldest = recent_completed[-1].timestamp
    if oldest and server_now - oldest <= timedelta(minutes=10):
        window.batch_completion_count += 1


def log_user_event(
    db: Session,
    user_id: int,
    event_type: str,
    step_id: str | UUID | None = None,
    content_id: str | UUID | None = None,
    plan_step_id: str | int | UUID | None = None,
    context: dict[str, Any] | None = None,
    plan_instance_id: str | UUID | None = None,
) -> UserEvent:
    server_now = _utc_now()
    user = db.get(User, user_id)
    if not user:
        raise ValueError(f"User {user_id} not found")

    local_dt = server_now.astimezone(_resolve_timezone(user))
    bucket = _time_bucket(local_dt)

    event_context = dict(context or {})
    if content_id is not None:
        event_context.setdefault("content_id", str(content_id))
    event_context.setdefault("timezone_source", "user_profile")

    instance = _ensure_plan_instance(db, user_id, plan_instance_id)
    window = _ensure_execution_window(db, instance, server_now)

    if window.engagement_status == EngagementStatus.DORMANT:
        window.engagement_status = EngagementStatus.RETURNING
        _record_plan_resumed_event(
            db,
            user_id,
            window,
            bucket,
            event_context,
            server_now,
        )

    is_task_event = event_type in TASK_EVENT_TYPES

    if is_task_event:
        step_value = _resolve_step_id(step_id, content_id, plan_step_id)
        if content_id is None and not db.get(ContentLibrary, step_value):
            _ensure_content_stub(db, step_value, event_context)
        content_lookup_id = str(content_id) if content_id is not None else step_value
        content = db.get(ContentLibrary, content_lookup_id)
        if content:
            event_context.setdefault("content_version", content.content_version)
    else:
        step_value = step_id and str(step_id) or (plan_step_id and str(plan_step_id)) or None

    if event_type == "parameter_set":
        if event_context.get("parameter") == "load_mode":
            window.current_load_mode = str(event_context.get("new_value") or window.current_load_mode)

    if event_type == "task_completed":
        _maybe_increment_batch_completion(db, window, server_now)

    event = UserEvent(
        event_type=event_type,
        timestamp=server_now,
        user_id=user_id,
        plan_execution_id=window.id,
        step_id=step_value,
        time_of_day_bucket=bucket,
        context=event_context,
    )
    db.add(event)

    if is_task_event and step_value:
        stats = _get_or_create_task_stats(db, user_id, step_value)
        _update_task_stats(stats, event_type, bucket, event_context)
        if event_type in FRICTION_EVENT_TYPES:
            stats.last_failure_reason = event_context.get("skip_reason") or stats.last_failure_reason
            stats.history_ref = True
            _maybe_create_failure_signal(
                db,
                user_id,
                window,
                step_value,
                event_type,
                event_context,
                server_now,
            )

    if event_type in {"task_skipped", "task_ignored", "task_failed"} and step_value:
        db.flush()
        _maybe_emit_system_prompt(db, user, window, event_type)

    return event




def get_success_streak(db: Session, user_id: int, limit: int = 60) -> int:
    events = (
        db.query(UserEvent.event_type)
        .filter(
            UserEvent.user_id == user_id,
            UserEvent.event_type.in_({"task_completed", "task_skipped", "task_ignored", "task_failed"}),
        )
        .order_by(UserEvent.timestamp.desc(), UserEvent.id.desc())
        .limit(limit)
        .all()
    )

    streak = 0
    for (event_type,) in events:
        if event_type == "task_completed":
            streak += 1
        else:
            break
    return streak

def get_skip_streak(db: Session, user_id: int, limit: int) -> int:
    events = (
        db.query(UserEvent.event_type)
        .filter(
            UserEvent.user_id == user_id,
            UserEvent.event_type.in_(SKIP_STREAK_EVENT_TYPES | SKIP_STREAK_RESET_EVENT_TYPES),
        )
        .order_by(UserEvent.timestamp.desc(), UserEvent.id.desc())
        .limit(limit)
        .all()
    )

    skip_streak = 0
    for (event_type,) in events:
        if event_type in SKIP_STREAK_RESET_EVENT_TYPES:
            break
        if event_type in SKIP_STREAK_EVENT_TYPES:
            skip_streak += 1

    return skip_streak


def get_completion_ratio(
    db: Session,
    user_id: int,
    days: int = 7,
    now: datetime | None = None,
) -> float:
    server_now = now or _utc_now()
    window_start = server_now - timedelta(days=days)
    completed_total = (
        db.query(func.count(UserEvent.id))
        .filter(
            UserEvent.user_id == user_id,
            UserEvent.timestamp >= window_start,
            UserEvent.event_type == "task_completed",
        )
        .scalar()
        or 0
    )
    total_attempts = (
        db.query(func.count(UserEvent.id))
        .filter(
            UserEvent.user_id == user_id,
            UserEvent.timestamp >= window_start,
            UserEvent.event_type.in_(COMPLETION_EVENT_TYPES),
        )
        .scalar()
        or 0
    )

    if total_attempts == 0:
        return 0.0
    return float(completed_total / total_attempts)


def get_friction_event_count(
    db: Session,
    user_id: int,
    days: int = 7,
    now: datetime | None = None,
) -> int:
    server_now = now or _utc_now()
    window_start = server_now - timedelta(days=days)
    return int(
        db.query(func.count(UserEvent.id))
        .filter(
            UserEvent.user_id == user_id,
            UserEvent.timestamp >= window_start,
            UserEvent.event_type.in_(FRICTION_EVENT_TYPES),
        )
        .scalar()
        or 0
    )


def update_engagement_statuses(db: Session, now: datetime | None = None) -> int:
    server_now = now or _utc_now()
    updates = 0

    windows = (
        db.query(PlanExecutionWindow)
        .filter(PlanExecutionWindow.end_date.is_(None))
        .all()
    )
    for window in windows:
        last_event_time = (
            db.query(func.max(UserEvent.timestamp))
            .filter(UserEvent.plan_execution_id == window.id)
            .scalar()
        )
        if not last_event_time:
            continue

        gap = server_now - last_event_time
        if gap < timedelta(hours=48):
            new_status = EngagementStatus.ACTIVE
        elif gap < timedelta(days=7):
            new_status = EngagementStatus.SPORADIC
        else:
            new_status = EngagementStatus.DORMANT

        if window.engagement_status != new_status:
            window.engagement_status = new_status
            updates += 1

    return updates


def update_hidden_compensation_scores(db: Session) -> int:
    windows = (
        db.query(PlanExecutionWindow)
        .filter(PlanExecutionWindow.end_date.is_(None))
        .all()
    )
    updated = 0
    for window in windows:
        completed_total = (
            db.query(func.count(UserEvent.id))
            .filter(
                UserEvent.plan_execution_id == window.id,
                UserEvent.event_type == "task_completed",
            )
            .scalar()
            or 0
        )
        if completed_total == 0:
            window.hidden_compensation_score = 0.0
            updated += 1
            continue

        completed_events = db.query(func.count(UserEvent.id)).filter(
            UserEvent.plan_execution_id == window.id,
            UserEvent.event_type == "task_completed",
        )
        night_total = (
            completed_events.filter(UserEvent.time_of_day_bucket == "night").scalar()
            or 0
        )
        edge_total = (
            completed_events.filter(UserEvent.context["is_edge_of_day"].astext == "true").scalar()
            or 0
        )
        score = (night_total + edge_total + window.batch_completion_count) / completed_total
        window.hidden_compensation_score = float(score)
        updated += 1

    return updated

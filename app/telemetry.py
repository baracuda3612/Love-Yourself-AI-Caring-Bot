"""Telemetry helpers for logging events and managing engagement status."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import pytz
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from app.db import (
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
TASK_EVENT_TYPES = {"task_delivered", "task_completed", "task_skipped", "task_ignored"}


def _coerce_uuid(value: str | UUID | None) -> UUID | None:
    if value is None or isinstance(value, UUID):
        return value
    return UUID(str(value))


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
    stats = TaskStats(user_id=user_id, step_id=step_id)
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
        stats.completed_total += 1
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


def _maybe_create_failure_signal(
    db: Session,
    user_id: int,
    window: PlanExecutionWindow,
    step_id: str,
    event_type: str,
    context: dict[str, Any],
    server_now: datetime,
) -> None:
    if event_type not in {"task_skipped", "task_ignored"}:
        return
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

    step_value = str(step_id) if step_id is not None else None
    if step_value:
        content = db.get(ContentLibrary, step_value)
        if content:
            event_context.setdefault("content_version", content.content_version)

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

    if step_value and event_type in TASK_EVENT_TYPES:
        stats = _get_or_create_task_stats(db, user_id, step_value)
        _update_task_stats(stats, event_type, bucket, event_context)
        if event_type in {"task_skipped", "task_ignored"}:
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

    return event


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

"""Plan metrics based on telemetry-delivered tasks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Integer, cast
from sqlalchemy.orm import Session

from app.db import AIPlan, AIPlanDay, AIPlanStep, UserEvent

DELIVERED_EVENT_TYPE = "task_delivered"
RESET_EVENT_TYPES = {"plan_adapted", "plan_restarted", "plan_created"}


@dataclass(frozen=True)
class _TimelineEvent:
    timestamp: datetime
    step: AIPlanStep | None
    is_reset: bool


def _plan_step_id_expr():
    return cast(UserEvent.context["plan_step_id"].astext, Integer)


def _plan_id_expr():
    return cast(UserEvent.context["plan_id"].astext, Integer)


def _fetch_delivered_steps(
    db: Session,
    user_id: int,
    plan_id: int,
) -> list[tuple[AIPlanStep, datetime]]:
    return (
        db.query(AIPlanStep, UserEvent.timestamp)
        .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
        .join(AIPlan, AIPlan.id == AIPlanDay.plan_id)
        .join(UserEvent, _plan_step_id_expr() == AIPlanStep.id)
        .filter(
            UserEvent.user_id == user_id,
            UserEvent.event_type == DELIVERED_EVENT_TYPE,
            AIPlan.id == plan_id,
        )
        .order_by(UserEvent.timestamp.desc())
        .all()
    )


def _fetch_reset_events(db: Session, user_id: int, plan_id: int) -> list[datetime]:
    return [
        timestamp
        for (timestamp,) in (
            db.query(UserEvent.timestamp)
            .filter(
                UserEvent.user_id == user_id,
                UserEvent.event_type.in_(RESET_EVENT_TYPES),
                _plan_id_expr() == plan_id,
            )
            .order_by(UserEvent.timestamp.desc())
            .all()
        )
    ]


def get_recent_tasks(
    db: Session,
    user_id: int,
    plan_id: int,
    limit: int,
) -> list[AIPlanStep]:
    delivered = _fetch_delivered_steps(db, user_id, plan_id)
    return [step for step, _timestamp in delivered[:limit]]


def get_completion_rate(db: Session, user_id: int, plan_id: int) -> float:
    delivered = _fetch_delivered_steps(db, user_id, plan_id)
    if not delivered:
        return 0.0
    completed = sum(1 for step, _timestamp in delivered if step.is_completed)
    return float(completed / len(delivered))


def calculate_skip_streak(db: Session, user_id: int, plan_id: int) -> int:
    delivered = _fetch_delivered_steps(db, user_id, plan_id)
    if not delivered:
        return 0

    timeline = [
        _TimelineEvent(timestamp=timestamp, step=step, is_reset=False)
        for step, timestamp in delivered
    ]
    timeline.extend(
        _TimelineEvent(timestamp=timestamp, step=None, is_reset=True)
        for timestamp in _fetch_reset_events(db, user_id, plan_id)
    )
    timeline.sort(key=lambda item: (item.timestamp, item.is_reset), reverse=True)

    skip_streak = 0
    for event in timeline:
        if event.is_reset:
            break
        if event.step is None:
            continue
        if event.step.is_completed:
            break
        if event.step.skipped:
            skip_streak += 1
            continue
        break

    return skip_streak

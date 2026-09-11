"""Plan metrics based on telemetry-delivered tasks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

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
    """Return the canonical integer plan-step linkage."""
    return UserEvent.plan_step_id


def _plan_id_expr():
    return UserEvent.plan_id


def fetch_delivered_steps(
    db: Session,
    user_id: int,
    plan_id: int,
) -> list[tuple[AIPlanStep, datetime]]:
    return (
        db.query(AIPlanStep, UserEvent.occurred_at)
        .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
        .join(AIPlan, AIPlan.id == AIPlanDay.plan_id)
        .join(UserEvent, _plan_step_id_expr() == AIPlanStep.id)
        .filter(
            UserEvent.user_id == user_id,
            UserEvent.event_name == DELIVERED_EVENT_TYPE,
            AIPlan.id == plan_id,
        )
        .order_by(UserEvent.occurred_at.desc())
        .all()
    )


def _fetch_reset_events(db: Session, user_id: int, plan_id: int) -> list[datetime]:
    return [
        timestamp
        for (timestamp,) in (
            db.query(UserEvent.occurred_at)
            .filter(
                UserEvent.user_id == user_id,
                UserEvent.event_name.in_(RESET_EVENT_TYPES),
                _plan_id_expr() == plan_id,
            )
            .order_by(UserEvent.occurred_at.desc())
            .all()
        )
    ]


def get_recent_tasks(
    db: Session,
    user_id: int,
    plan_id: int,
    limit: int,
) -> list[AIPlanStep]:
    delivered = fetch_delivered_steps(db, user_id, plan_id)
    return [step for step, _timestamp in delivered[:limit]]


def get_completion_rate(db: Session, user_id: int, plan_id: int) -> float:
    """
    completion_rate = completed / eligible
    eligible = steps where step_status in (completed, skipped, expired)
               AND scheduled_at <= now
    Future pending/delivered tasks are excluded.
    """
    from app.db import AIPlanDay
    import pytz as _pytz
    now_utc = datetime.now(_pytz.UTC)
    eligible_steps = (
        db.query(AIPlanStep)
        .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
        .filter(
            AIPlanDay.plan_id == plan_id,
            AIPlanStep.step_status.in_(["completed", "skipped", "expired"]),
            AIPlanStep.scheduled_for <= now_utc,
        )
        .all()
    )
    if not eligible_steps:
        return 0.0
    total = len(eligible_steps)
    completed = sum(1 for s in eligible_steps if s.step_status == "completed")
    return float(completed / total)


def _fetch_eligible_steps(db: Session, plan_id: int):
    """Shared helper: eligible steps for all rate calculations."""
    from app.db import AIPlanDay
    import pytz as _pytz
    now_utc = datetime.now(_pytz.UTC)
    return (
        db.query(AIPlanStep)
        .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
        .filter(
            AIPlanDay.plan_id == plan_id,
            AIPlanStep.step_status.in_(["completed", "skipped", "expired"]),
            AIPlanStep.scheduled_for <= now_utc,
        )
        .all()
    )


def get_engagement_rate(db: Session, user_id: int, plan_id: int) -> float:
    """
    engagement_rate = (completed + skipped) / eligible
    Measures whether the user interacts at all, regardless of outcome.
    skipped can be healthy autonomy; expired is disengagement.
    """
    steps = _fetch_eligible_steps(db, plan_id)
    if not steps:
        return 0.0
    acted = sum(1 for s in steps if s.step_status in ("completed", "skipped"))
    return float(acted / len(steps))


def get_silent_miss_rate(db: Session, user_id: int, plan_id: int) -> float:
    """
    silent_miss_rate = expired / eligible
    Primary churn signal: user had a window, did not react at all.
    Higher value → risk of disengagement.
    """
    steps = _fetch_eligible_steps(db, plan_id)
    if not steps:
        return 0.0
    missed = sum(1 for s in steps if s.step_status == "expired")
    return float(missed / len(steps))


def calculate_skip_streak(db: Session, user_id: int, plan_id: int) -> int:
    delivered = fetch_delivered_steps(db, user_id, plan_id)
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
        if event.step.step_status == "completed":
            break
        if event.step.step_status == "skipped":
            skip_streak += 1
            continue
        continue

    return skip_streak

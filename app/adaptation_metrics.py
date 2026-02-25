"""Adaptation analytics queries over AdaptationHistory and UserEvent."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db import AIPlan, AdaptationHistory, UserEvent
from app.telemetry import get_completion_ratio


def get_adaptation_count(
    db: Session,
    plan_id: int,
    category: str | None = None,
    include_rolled_back: bool = False,
) -> int:
    """Total number of adaptations applied to a plan."""
    q = db.query(func.count(AdaptationHistory.id)).filter(
        AdaptationHistory.plan_id == plan_id,
    )
    if not include_rolled_back:
        q = q.filter(AdaptationHistory.is_rolled_back.is_(False))
    if category:
        q = q.filter(AdaptationHistory.category == category)
    return int(q.scalar() or 0)


def get_adaptations_by_category(
    db: Session,
    plan_id: int,
    include_rolled_back: bool = False,
) -> dict[str, int]:
    """Count of adaptations grouped by category for a plan."""
    q = (
        db.query(AdaptationHistory.category, func.count(AdaptationHistory.id))
        .filter(AdaptationHistory.plan_id == plan_id)
    )
    if not include_rolled_back:
        q = q.filter(AdaptationHistory.is_rolled_back.is_(False))
    rows = q.group_by(AdaptationHistory.category).all()
    return {row[0]: row[1] for row in rows}


def get_recent_adaptations(
    db: Session,
    plan_id: int,
    limit: int = 10,
) -> list[AdaptationHistory]:
    """Last N adaptation history entries for a plan, newest first."""
    return (
        db.query(AdaptationHistory)
        .filter(AdaptationHistory.plan_id == plan_id)
        .order_by(AdaptationHistory.applied_at.desc())
        .limit(limit)
        .all()
    )


def get_undo_rate(db: Session, plan_id: int) -> float:
    """Fraction of adaptations that were rolled back.

    Returns 0.0 if no adaptations exist.
    High undo rate = poor adaptation prediction quality.
    """
    total = db.query(func.count(AdaptationHistory.id)).filter(
        AdaptationHistory.plan_id == plan_id
    ).scalar() or 0
    if total == 0:
        return 0.0
    rolled_back = db.query(func.count(AdaptationHistory.id)).filter(
        AdaptationHistory.plan_id == plan_id,
        AdaptationHistory.is_rolled_back.is_(True),
    ).scalar() or 0
    return float(rolled_back / total)


def get_adaptation_acceptance_rate(
    db: Session,
    user_id: int,
    plan_id: int,
) -> float | None:
    """Ratio of executed adaptations to proposed adaptations for this plan.

    Returns None if no proposals have been logged (can't distinguish 0% from unknown).
    > 1.0 is possible: user can self-initiate adaptations without a proposal.
    Range [0.0, ∞). Values > 1 mean user is more active than system-prompted.
    """
    plan = db.query(AIPlan).filter(AIPlan.id == plan_id).first()
    if not plan:
        return None

    proposed = (
        db.query(func.count(UserEvent.id))
        .filter(
            UserEvent.user_id == user_id,
            UserEvent.event_type == "adaptation_proposed",
            UserEvent.context["plan_id"].astext == str(plan_id),
        )
        .scalar() or 0
    )
    if proposed == 0:
        return None

    executed = get_adaptation_count(db, plan_id)
    return float(executed / proposed)


def get_completion_rate_delta(
    db: Session,
    user_id: int,
    applied_at: datetime,
    window_days: int = 7,
) -> float | None:
    """Compare 7-day completion rate before vs after an adaptation timestamp.

    Returns delta (after - before). Positive = improvement. None if insufficient data.
    Uses get_completion_ratio from telemetry which already handles empty windows.
    """
    now_utc = datetime.now(timezone.utc)

    before_end = applied_at
    after_end = applied_at + timedelta(days=window_days)

    if after_end > now_utc:
        return None

    rate_before = get_completion_ratio(db, user_id, days=window_days, now=before_end)
    rate_after = get_completion_ratio(db, user_id, days=window_days, now=after_end)

    return round(rate_after - rate_before, 4)


def get_most_frequent_intent(
    db: Session,
    plan_id: int,
    include_rolled_back: bool = False,
) -> str | None:
    """Most frequently applied adaptation intent for a plan.

    Returns None if no adaptations exist.
    """
    q = (
        db.query(AdaptationHistory.intent, func.count(AdaptationHistory.id).label("cnt"))
        .filter(AdaptationHistory.plan_id == plan_id)
    )
    if not include_rolled_back:
        q = q.filter(AdaptationHistory.is_rolled_back.is_(False))
    row = q.group_by(AdaptationHistory.intent).order_by(func.count(AdaptationHistory.id).desc()).first()
    return row[0] if row else None


def get_adaptation_velocity(
    db: Session,
    plan_id: int,
    days: int = 30,
    now: datetime | None = None,
) -> float:
    """Adaptations per week over the last N days.

    Useful for detecting over-adaptation (too many changes = plan instability).
    """
    now_utc = now or datetime.now(timezone.utc)
    since = now_utc - timedelta(days=days)
    count = (
        db.query(func.count(AdaptationHistory.id))
        .filter(
            AdaptationHistory.plan_id == plan_id,
            AdaptationHistory.applied_at >= since,
            AdaptationHistory.is_rolled_back.is_(False),
        )
        .scalar() or 0
    )
    weeks = days / 7.0
    return round(float(count) / weeks, 2) if weeks > 0 else 0.0

"""Read the next scheduled reminder shared by the menu and the AI assistant."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.db import AIPlanDay, AIPlanStep
from app.plan_completion.chronology import utc


def read_next_reminder(
    db: Session,
    plan_id: int,
    *,
    paused: bool,
    timezone_name: str | None,
    delivery: bool,
    now: datetime | None = None,
) -> tuple[str, datetime | None]:
    if paused:
        return "paused", None
    if not delivery:
        return "delivery_disabled", None
    if not timezone_name:
        return "timezone_unavailable", None
    try:
        zone = ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError):
        return "timezone_unavailable", None
    next_step = (
        db.query(AIPlanStep.scheduled_for)
        .join(AIPlanDay)
        .filter(
            AIPlanDay.plan_id == plan_id,
            AIPlanStep.step_status == "pending",
            AIPlanStep.scheduled_for > (now or datetime.now(timezone.utc)),
        )
        .order_by(AIPlanStep.scheduled_for)
        .first()
    )
    if next_step is None:
        return "no_upcoming_reminder", None
    return "scheduled", utc(next_step.scheduled_for).astimezone(zone)

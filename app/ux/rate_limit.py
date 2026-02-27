"""Auto-message rate limiting — prevent overwhelming users."""
from __future__ import annotations

from datetime import date

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db import UserEvent

AUTO_MESSAGE_EVENT_TYPES = {
    "pulse_sent",
    "silent_sent",
    "task_delivered",
}

MAX_AUTO_MESSAGES_PER_DAY = 4


def can_send_auto_message(db: Session, user_id: int, event_type: str) -> bool:
    """Return True if user can receive another auto message today."""
    if event_type == "task_delivered":
        return True

    if event_type == "silent_sent":
        pulse_today = db.query(UserEvent).filter(
            UserEvent.user_id == user_id,
            UserEvent.event_type == "pulse_sent",
            func.date(UserEvent.timestamp) == date.today(),
        ).first()
        if pulse_today:
            return False

    return True

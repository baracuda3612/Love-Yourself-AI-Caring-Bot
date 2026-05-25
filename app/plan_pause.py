"""
Plan pause mechanics (T5.1).

Simple pause / resume — no plan rewrite, no adaptation record.

Invariants:
  - Pause is NOT an adaptation. No AdaptationHistory record is created.
  - No plan steps are modified or rescheduled.
  - ADAPTATIONS_ENABLED gate does not apply to pause/resume.

Delivery gate:
  The scheduler already skips delivery for current_state != "ACTIVE".
  pause_plan() sets current_state = "ACTIVE_PAUSED" which is sufficient for
  the scheduler's can_deliver_tasks() check.
  UserProfile.is_paused is the authoritative persistent flag (survives restarts)
  and is used for analytics and reporting.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

if TYPE_CHECKING:  # pragma: no cover
    from app.db import User, UserProfile


# ─── Errors ──────────────────────────────────────────────────────────────────


class PlanNotActiveError(RuntimeError):
    """User is not in ACTIVE state — cannot pause."""


class PlanAlreadyPausedError(RuntimeError):
    """Plan is already paused."""


class PlanNotPausedError(RuntimeError):
    """Plan is not paused — cannot resume."""


# ─── Public API ───────────────────────────────────────────────────────────────


def pause_plan(db: Session, user_id: int) -> "UserProfile":
    """
    Pause plan delivery for a user.

    Changes:
        user_profiles.is_paused    = True
        user_profiles.pause_count += 1   (never decrements)
        users.current_state        = "ACTIVE_PAUSED"

    Does NOT rewrite or reschedule any plan steps.
    Does NOT create an AdaptationHistory record.
    Caller must db.commit() after this call.

    Raises:
        PlanNotActiveError     if user is not in ACTIVE state
        PlanAlreadyPausedError if already paused
    """
    from app.db import User, UserProfile  # lazy import — avoids engine init at import time

    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise PlanNotActiveError(f"User {user_id} not found")
    if user.current_state != "ACTIVE":
        raise PlanNotActiveError(
            f"User {user_id} cannot be paused: state={user.current_state!r} "
            f"(expected ACTIVE)"
        )

    profile = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
    if profile is None:
        raise PlanNotActiveError(f"No UserProfile for user {user_id}")
    if profile.is_paused:
        raise PlanAlreadyPausedError(f"User {user_id} is already paused")

    profile.is_paused = True
    profile.pause_count = (profile.pause_count or 0) + 1
    user.current_state = "ACTIVE_PAUSED"
    db.flush()
    return profile


def resume_plan(db: Session, user_id: int) -> "UserProfile":
    """
    Resume plan delivery for a user.

    Changes:
        user_profiles.is_paused = False
        users.current_state     = "ACTIVE"

    Does NOT create an AdaptationHistory record.
    Caller must db.commit() after this call.

    Raises:
        PlanNotPausedError if the user's plan is not currently paused
    """
    from app.db import User, UserProfile  # lazy import

    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise PlanNotPausedError(f"User {user_id} not found")

    profile = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
    if profile is None:
        raise PlanNotPausedError(f"No UserProfile for user {user_id}")
    if not profile.is_paused:
        raise PlanNotPausedError(
            f"User {user_id} is not paused (is_paused=False)"
        )

    profile.is_paused = False
    user.current_state = "ACTIVE"
    db.flush()
    return profile

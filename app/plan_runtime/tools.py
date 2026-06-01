"""
Plan runtime tools — callable by Coach agent.

Each function is self-contained: opens its own DB session, enforces invariants,
commits FSM transition when needed. Returns a plain dict result.

All DB / external imports are lazy (inside function bodies) — mirrors the
pattern in app/plan_pause.py so unit tests can stub those modules before
importing this module.

Tool registration in Coach prompt is T5.7.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_HHMM_RE = re.compile(r"^\d{2}:\d{2}$")

_FOLLOWUP_STATES = {"IDLE_FINISHED", "IDLE_DROPPED", "IDLE_PLAN_ABORTED"}


def _validate_hhmm(hhmm: str) -> None:
    """Raise ValueError if hhmm does not match HH:MM (exactly 2-digit each part)."""
    if not _HHMM_RE.match(hhmm):
        raise ValueError(
            f"Invalid time format {hhmm!r} — expected HH:MM (e.g. '09:30')"
        )


def _load_user_and_profile(db, user_id: int):
    """Return (user, profile) or raise ValueError if user not found."""
    from app.db import User, UserProfile  # lazy

    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise ValueError(f"User {user_id} not found")
    profile = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
    return user, profile


def _get_active_plan(db, user_id: int):
    """Return the active or paused plan for user_id, or None."""
    from app.db import AIPlan  # lazy

    return (
        db.query(AIPlan)
        .filter(AIPlan.user_id == user_id, AIPlan.status.in_(["active", "paused"]))
        .order_by(AIPlan.created_at.desc())
        .first()
    )


def _get_future_slot_step_ids(db, user_id: int, time_slot: str) -> list:
    """Return IDs of future pending/delivered steps in the given time_slot."""
    from app.db import AIPlan, AIPlanDay, AIPlanStep  # lazy

    plan = _get_active_plan(db, user_id)
    if plan is None:
        return []

    now_utc = datetime.now(timezone.utc)

    rows = (
        db.query(AIPlanStep.id)
        .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
        .filter(
            AIPlanDay.plan_id == plan.id,
            AIPlanStep.time_slot == time_slot,
            AIPlanStep.step_status.in_(["pending", "delivered"]),
            AIPlanStep.scheduled_for > now_utc,
        )
        .all()
    )
    return [row[0] for row in rows]


# ─── Public tools ─────────────────────────────────────────────────────────────


def create_first_plan(user_id: int) -> dict:
    """Create the first (SHORT) plan for a freshly onboarded user.

    Invariants:
      - user.current_state must be IDLE_ONBOARDED
      - profile.daily_time_slots["DAY"] must be set
      - Sets user.current_state = "ACTIVE" after plan creation
    """
    from app.db import SessionLocal, User, UserProfile  # lazy
    from app.plan_drafts.service import create_plan  # lazy

    with SessionLocal() as db:
        user, profile = _load_user_and_profile(db, user_id)

        if user.current_state != "IDLE_ONBOARDED":
            raise ValueError(
                f"create_first_plan only allowed from IDLE_ONBOARDED, "
                f"got {user.current_state!r}"
            )

        time_slots: dict = (profile.daily_time_slots or {}) if profile else {}
        day_time: Optional[str] = time_slots.get("DAY")
        if not day_time:
            raise ValueError("day_time required for plan creation")

        plan = create_plan(
            db,
            user_id=user_id,
            plan_type="SHORT",
            day_time=day_time,
            evening_time=None,
        )

        # Re-fetch user inside the same session after create_plan flushes
        user = db.query(User).filter(User.id == user_id).first()
        user.current_state = "ACTIVE"
        db.add(user)
        db.commit()

    logger.info("[plan_runtime] create_first_plan: user=%s plan_id=%s", user_id, plan.id)
    return {"status": "ok", "plan_type": "SHORT"}


def create_followup_plan(user_id: int, plan_type: str) -> dict:
    """Create a follow-up plan after a plan has ended.

    plan_type must be 'SHORT' or 'MEDIUM'.
    For MEDIUM, profile.evening_slot_collected must be True; otherwise returns
    {"status": "needs_evening_time"} (caller should collect evening time first).

    Invariants:
      - user.current_state in {IDLE_FINISHED, IDLE_DROPPED, IDLE_PLAN_ABORTED}
      - Sets user.current_state = "ACTIVE" on success
    """
    if plan_type not in {"SHORT", "MEDIUM"}:
        raise ValueError(f"plan_type must be 'SHORT' or 'MEDIUM', got {plan_type!r}")

    from app.db import SessionLocal, User  # lazy
    from app.plan_drafts.service import create_plan  # lazy

    with SessionLocal() as db:
        user, profile = _load_user_and_profile(db, user_id)

        if user.current_state not in _FOLLOWUP_STATES:
            raise ValueError(
                f"create_followup_plan only allowed from {_FOLLOWUP_STATES}, "
                f"got {user.current_state!r}"
            )

        time_slots: dict = (profile.daily_time_slots or {}) if profile else {}
        day_time: Optional[str] = time_slots.get("DAY") or "14:00"

        evening_time: Optional[str] = None
        if plan_type == "MEDIUM":
            if not (profile and profile.evening_slot_collected):
                return {"status": "needs_evening_time"}
            evening_time = time_slots.get("EVENING")

        plan = create_plan(
            db,
            user_id=user_id,
            plan_type=plan_type,
            day_time=day_time,
            evening_time=evening_time,
        )

        user = db.query(User).filter(User.id == user_id).first()
        user.current_state = "ACTIVE"
        db.add(user)
        db.commit()

    logger.info(
        "[plan_runtime] create_followup_plan: user=%s plan_id=%s type=%s",
        user_id, plan.id, plan_type,
    )
    return {"status": "ok", "plan_type": plan_type}


def record_evening_time(user_id: int, hhmm: str) -> dict:
    """Persist the user's chosen evening delivery time and mark slot as collected.

    Used before creating a MEDIUM plan for the first time.
    """
    _validate_hhmm(hhmm)

    from app.db import SessionLocal, UserProfile  # lazy

    with SessionLocal() as db:
        user, profile = _load_user_and_profile(db, user_id)

        if profile is None:
            profile = UserProfile(user_id=user_id)
            db.add(profile)

        time_slots: dict = dict(profile.daily_time_slots or {})
        time_slots["EVENING"] = hhmm
        profile.daily_time_slots = time_slots
        profile.evening_slot_collected = True
        db.add(profile)
        db.commit()

    logger.info("[plan_runtime] record_evening_time: user=%s hhmm=%s", user_id, hhmm)
    return {"status": "ok", "evening_time": hhmm}


def change_day_time(user_id: int, hhmm: str) -> dict:
    """Change the DAY slot delivery time and reschedule pending/delivered steps.

    Updates profile.daily_time_slots["DAY"] and reschedules all future DAY-slot
    steps on the active plan.
    """
    _validate_hhmm(hhmm)

    from app.db import SessionLocal  # lazy
    from app.scheduler import reschedule_plan_steps  # lazy

    with SessionLocal() as db:
        user, profile = _load_user_and_profile(db, user_id)

        if profile is None:
            from app.db import UserProfile  # lazy
            profile = UserProfile(user_id=user_id)
            db.add(profile)

        time_slots: dict = dict(profile.daily_time_slots or {})
        time_slots["DAY"] = hhmm
        profile.daily_time_slots = time_slots
        db.add(profile)
        db.commit()

        step_ids = _get_future_slot_step_ids(db, user_id, "DAY")

    rescheduled = 0
    if step_ids:
        rescheduled = reschedule_plan_steps(step_ids)

    logger.info(
        "[plan_runtime] change_day_time: user=%s hhmm=%s rescheduled=%d",
        user_id, hhmm, rescheduled,
    )
    return {"status": "ok", "day_time": hhmm, "rescheduled": rescheduled}


def change_evening_time(user_id: int, hhmm: str) -> dict:
    """Change the EVENING slot delivery time and reschedule pending/delivered steps.

    Updates profile.daily_time_slots["EVENING"] and reschedules future EVENING steps.
    """
    _validate_hhmm(hhmm)

    from app.db import SessionLocal  # lazy
    from app.scheduler import reschedule_plan_steps  # lazy

    with SessionLocal() as db:
        user, profile = _load_user_and_profile(db, user_id)

        if profile is None:
            from app.db import UserProfile  # lazy
            profile = UserProfile(user_id=user_id)
            db.add(profile)

        time_slots: dict = dict(profile.daily_time_slots or {})
        time_slots["EVENING"] = hhmm
        profile.daily_time_slots = time_slots
        db.add(profile)
        db.commit()

        step_ids = _get_future_slot_step_ids(db, user_id, "EVENING")

    rescheduled = 0
    if step_ids:
        rescheduled = reschedule_plan_steps(step_ids)

    logger.info(
        "[plan_runtime] change_evening_time: user=%s hhmm=%s rescheduled=%d",
        user_id, hhmm, rescheduled,
    )
    return {"status": "ok", "evening_time": hhmm, "rescheduled": rescheduled}


def get_plan_status(user_id: int) -> dict:
    """Return current FSM state and active plan summary for user_id."""
    from app.db import SessionLocal  # lazy

    with SessionLocal() as db:
        user, _ = _load_user_and_profile(db, user_id)
        plan = _get_active_plan(db, user_id)

        if plan is None:
            return {"state": user.current_state, "plan_active": False}

        days_total = plan.total_days or 0
        days_completed = max(0, (plan.current_day or 1) - 1)

        return {
            "state": user.current_state,
            "plan_active": True,
            "days_total": days_total,
            "days_completed": days_completed,
        }


def pause_plan(user_id: int) -> dict:
    """Pause an active plan.

    Delegates to app.plan_pause.pause_plan() which sets:
      profile.is_paused = True, profile.pause_count += 1,
      user.current_state = "ACTIVE_PAUSED"

    Raises ValueError if user is not in ACTIVE state.
    """
    from app.db import SessionLocal  # lazy
    from app.plan_pause import (  # lazy
        PlanAlreadyPausedError,
        PlanNotActiveError,
        pause_plan as _pause_plan,
    )

    with SessionLocal() as db:
        user, _ = _load_user_and_profile(db, user_id)
        if user.current_state != "ACTIVE":
            raise ValueError(
                f"pause_plan requires ACTIVE state, got {user.current_state!r}"
            )
        try:
            _pause_plan(db, user_id)
        except (PlanNotActiveError, PlanAlreadyPausedError) as exc:
            raise ValueError(str(exc)) from exc
        db.commit()

    logger.info("[plan_runtime] pause_plan: user=%s", user_id)
    return {"status": "ok"}


def resume_plan(user_id: int) -> dict:
    """Resume a paused plan.

    Delegates to app.plan_pause.resume_plan() which sets:
      profile.is_paused = False, user.current_state = "ACTIVE"

    Raises ValueError if user is not in ACTIVE_PAUSED state.
    """
    from app.db import SessionLocal  # lazy
    from app.plan_pause import PlanNotPausedError, resume_plan as _resume_plan  # lazy

    with SessionLocal() as db:
        user, _ = _load_user_and_profile(db, user_id)
        if user.current_state != "ACTIVE_PAUSED":
            raise ValueError(
                f"resume_plan requires ACTIVE_PAUSED state, got {user.current_state!r}"
            )
        try:
            _resume_plan(db, user_id)
        except PlanNotPausedError as exc:
            raise ValueError(str(exc)) from exc
        db.commit()

    logger.info("[plan_runtime] resume_plan: user=%s", user_id)
    return {"status": "ok"}

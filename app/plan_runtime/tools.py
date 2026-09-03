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

def _validate_hhmm(hhmm: str) -> None:
    """Raise ValueError if hhmm does not match HH:MM (exactly 2-digit each part)."""
    if not _HHMM_RE.match(hhmm):
        raise ValueError(
            f"Invalid time format {hhmm!r} — expected HH:MM (e.g. '09:30')"
        )


def _load_user_and_profile(db, user_id: int, *, lock: bool = False):
    """Return (user, profile) or raise ValueError if user not found."""
    from app.db import User, UserProfile  # lazy

    user_query = db.query(User).filter(User.id == user_id)
    if lock:
        user_query = user_query.with_for_update()
    user = user_query.first()
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
        .one_or_none()
    )


def _existing_activation_retry(db, user_id: int, source_operation_id: str):
    """Return the plan for a committed activation retry, or None."""
    from app.db import AIPlan  # lazy
    from app.lifecycle import find_lifecycle_operation  # lazy

    receipt = find_lifecycle_operation(db, user_id, source_operation_id)
    if receipt is None:
        return None
    if receipt.operation != "activate":
        raise ValueError(
            "source_operation_id already belongs to "
            f"{receipt.operation}, not activate"
        )
    plan = (
        db.query(AIPlan)
        .filter(AIPlan.id == receipt.plan_id, AIPlan.user_id == user_id)
        .first()
    )
    if plan is None:
        raise ValueError("activation_receipt_plan_missing")
    return plan



# ─── Public tools ─────────────────────────────────────────────────────────────


def create_first_plan(user_id: int, *, source_operation_id: str) -> dict:
    """Create the first (SHORT) plan for a freshly onboarded user.

    Requires completed onboarding, no plan history, and a DAY slot.
    """
    from app.db import AIPlan, SessionLocal  # lazy
    from app.lifecycle import CurrentMode, derive_current_mode  # lazy
    from app.plan_drafts.service import create_plan  # lazy

    with SessionLocal() as db:
        user, profile = _load_user_and_profile(db, user_id, lock=True)

        retried_plan = _existing_activation_retry(db, user_id, source_operation_id)
        if retried_plan is not None:
            return {"status": "ok", "plan_type": "SHORT", "duplicate": True}

        mode = derive_current_mode(db, user_id)
        if mode is not CurrentMode.NO_ACTIVE_PLAN:
            raise ValueError(f"create_first_plan requires completed onboarding, got {mode.value}")
        if db.query(AIPlan.id).filter(AIPlan.user_id == user_id).first() is not None:
            raise ValueError("create_first_plan requires empty plan history")

        time_slots: dict = (profile.daily_time_slots or {}) if profile else {}
        day_time: Optional[str] = time_slots.get("DAY")
        if not day_time:
            raise ValueError("day_time required for plan creation")

        activation = create_plan(
            db,
            user_id=user_id,
            plan_type="SHORT",
            day_time=day_time,
            evening_time=None,
            source_operation_id=source_operation_id,
        )

        db.commit()

    plan = activation.plan
    if not activation.duplicate:
        from app.plan_finalization import activate_plan_side_effects  # lazy
        activate_plan_side_effects(plan.id, user_id)

    logger.info("[plan_runtime] create_first_plan: user=%s plan_id=%s", user_id, plan.id)
    return {
        "status": "ok",
        "plan_type": "SHORT",
        "duplicate": activation.duplicate,
    }


def create_followup_plan(
    user_id: int,
    plan_type: str,
    *,
    source_operation_id: str,
) -> dict:
    """Create a follow-up plan after a plan has ended.

    plan_type must be 'SHORT' or 'MEDIUM'.
    For MEDIUM, profile.evening_slot_collected must be True; otherwise returns
    {"status": "needs_evening_time"} (caller should collect evening time first).

    Requires no current plan and at least one historical plan.
    """
    if plan_type not in {"SHORT", "MEDIUM"}:
        raise ValueError(f"plan_type must be 'SHORT' or 'MEDIUM', got {plan_type!r}")

    from app.db import AIPlan, SessionLocal  # lazy
    from app.lifecycle import CurrentMode, derive_current_mode  # lazy
    from app.plan_drafts.service import create_plan  # lazy

    with SessionLocal() as db:
        user, profile = _load_user_and_profile(db, user_id, lock=True)

        retried_plan = _existing_activation_retry(db, user_id, source_operation_id)
        if retried_plan is not None:
            retried_type = "MEDIUM" if retried_plan.total_days == 14 else "SHORT"
            return {
                "status": "ok",
                "plan_type": retried_type,
                "duplicate": True,
            }

        mode = derive_current_mode(db, user_id)
        if mode is not CurrentMode.NO_ACTIVE_PLAN:
            raise ValueError(f"create_followup_plan requires NO_ACTIVE_PLAN, got {mode.value}")
        if db.query(AIPlan.id).filter(AIPlan.user_id == user_id).first() is None:
            raise ValueError("create_followup_plan requires plan history")

        time_slots: dict = (profile.daily_time_slots or {}) if profile else {}
        day_time: Optional[str] = time_slots.get("DAY") or "14:00"

        evening_time: Optional[str] = None
        if plan_type == "MEDIUM":
            if not (profile and profile.evening_slot_collected):
                return {"status": "needs_evening_time"}
            evening_time = time_slots.get("EVENING")

        activation = create_plan(
            db,
            user_id=user_id,
            plan_type=plan_type,
            day_time=day_time,
            evening_time=evening_time,
            source_operation_id=source_operation_id,
        )

        db.commit()

    plan = activation.plan
    if not activation.duplicate:
        from app.plan_finalization import activate_plan_side_effects  # lazy
        activate_plan_side_effects(plan.id, user_id)

    logger.info(
        "[plan_runtime] create_followup_plan: user=%s plan_id=%s type=%s",
        user_id, plan.id, plan_type,
    )
    return {
        "status": "ok",
        "plan_type": plan_type,
        "duplicate": activation.duplicate,
    }


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

    Updates profile.daily_time_slots["DAY"], rewrites scheduled_for on all
    future pending steps via update_user_time_slots, then reschedules jobs.
    """
    _validate_hhmm(hhmm)

    from app.db import SessionLocal  # lazy
    from app.time_slots import TimeSlotError, update_user_time_slots  # lazy
    from app.scheduler import reschedule_plan_steps  # lazy

    with SessionLocal() as db:
        user, _ = _load_user_and_profile(db, user_id)
        try:
            _, active_ids = update_user_time_slots(db, user, {"DAY": hhmm})
        except TimeSlotError as exc:
            raise ValueError(str(exc)) from exc
        db.commit()

    rescheduled = reschedule_plan_steps(active_ids) if active_ids else 0
    logger.info(
        "[plan_runtime] change_day_time: user=%s hhmm=%s rescheduled=%d",
        user_id, hhmm, rescheduled,
    )
    return {"status": "ok", "day_time": hhmm, "rescheduled": rescheduled}


def change_evening_time(user_id: int, hhmm: str) -> dict:
    """Change the EVENING slot delivery time and reschedule pending/delivered steps.

    Updates profile.daily_time_slots["EVENING"], rewrites scheduled_for on all
    future pending steps via update_user_time_slots, then reschedules jobs.
    """
    _validate_hhmm(hhmm)

    from app.db import SessionLocal  # lazy
    from app.time_slots import TimeSlotError, update_user_time_slots  # lazy
    from app.scheduler import reschedule_plan_steps  # lazy

    with SessionLocal() as db:
        user, _ = _load_user_and_profile(db, user_id)
        try:
            _, active_ids = update_user_time_slots(db, user, {"EVENING": hhmm})
        except TimeSlotError as exc:
            raise ValueError(str(exc)) from exc
        db.commit()

    rescheduled = reschedule_plan_steps(active_ids) if active_ids else 0
    logger.info(
        "[plan_runtime] change_evening_time: user=%s hhmm=%s rescheduled=%d",
        user_id, hhmm, rescheduled,
    )
    return {"status": "ok", "evening_time": hhmm, "rescheduled": rescheduled}


def cancel_plan(user_id: int, *, source_operation_id: str) -> dict:
    """Atomically abandon the current plan and cancel its open steps."""
    from app.db import SessionLocal  # lazy
    from app.lifecycle import LifecycleTransitionError, abandon_current_plan  # lazy
    from app.scheduler import cancel_plan_step_jobs  # lazy

    with SessionLocal() as db:
        _load_user_and_profile(db, user_id)
        plan = _get_active_plan(db, user_id)
        total_days = int(plan.total_days) if plan and plan.total_days is not None else None
        try:
            result, step_ids = abandon_current_plan(
                db,
                user_id=user_id,
                source_operation_id=source_operation_id,
            )
        except LifecycleTransitionError as exc:
            raise ValueError(str(exc)) from exc
        db.commit()

    if step_ids:
        cancel_plan_step_jobs(step_ids)

    logger.info("[plan_runtime] cancel_plan: user=%s step_ids_canceled=%d", user_id, len(step_ids))
    return {"status": "ok", "total_days": total_days, "duplicate": result.duplicate}


def get_plan_status(user_id: int) -> dict:
    """Return the one derived current mode and current-plan summary."""
    from app.db import SessionLocal  # lazy
    from app.lifecycle import derive_current_day, derive_current_mode  # lazy

    with SessionLocal() as db:
        user, _ = _load_user_and_profile(db, user_id)
        plan = _get_active_plan(db, user_id)
        mode = derive_current_mode(db, user_id).value

        if plan is None:
            return {"state": mode, "current_mode": mode, "plan_active": False}

        days_total = max(0, int(plan.total_days or 0))
        current_day = derive_current_day(db, plan.id, days_total) if days_total else 1
        days_completed = max(0, current_day - 1)
        days_remaining = max(0, days_total - current_day + 1)

        eligible_steps = [
            step
            for day in list(getattr(plan, "days", []) or [])
            for step in list(getattr(day, "steps", []) or [])
            if getattr(step, "step_status", None) != "canceled"
        ]
        steps_total = len(eligible_steps)
        steps_completed = sum(
            1 for step in eligible_steps if getattr(step, "step_status", None) == "completed"
        )
        completion_rate = round((steps_completed / steps_total) * 100) if steps_total else 0

        return {
            "state": mode,
            "current_mode": mode,
            "plan_active": True,
            "days_total": days_total,
            "current_day": current_day,
            "days_completed": days_completed,
            "days_remaining": days_remaining,
            "steps_total": steps_total,
            "steps_completed": steps_completed,
            "completion_rate": completion_rate,
        }


def pause_plan(user_id: int, *, source_operation_id: str) -> dict:
    """Pause an active plan.

    Delegates to the plan-centric aggregate operation.
    """
    from app.db import SessionLocal  # lazy
    from app.plan_pause import (  # lazy
        PlanAlreadyPausedError,
        PlanNotActiveError,
        pause_plan as _pause_plan,
    )

    with SessionLocal() as db:
        _load_user_and_profile(db, user_id)
        try:
            result = _pause_plan(
                db, user_id, source_operation_id=source_operation_id
            )
        except (PlanNotActiveError, PlanAlreadyPausedError) as exc:
            raise ValueError(str(exc)) from exc
        db.commit()

    logger.info("[plan_runtime] pause_plan: user=%s", user_id)
    return {"status": "ok", "duplicate": result.duplicate}


def resume_plan(user_id: int, *, source_operation_id: str) -> dict:
    """Resume a paused plan.

    Delegates to the plan-centric aggregate operation.
    """
    from app.db import SessionLocal  # lazy
    from app.plan_pause import PlanNotPausedError, resume_plan as _resume_plan  # lazy

    with SessionLocal() as db:
        _load_user_and_profile(db, user_id)
        try:
            result = _resume_plan(
                db, user_id, source_operation_id=source_operation_id
            )
        except PlanNotPausedError as exc:
            raise ValueError(str(exc)) from exc
        db.commit()

    logger.info("[plan_runtime] resume_plan: user=%s", user_id)
    return {"status": "ok", "duplicate": result.duplicate}

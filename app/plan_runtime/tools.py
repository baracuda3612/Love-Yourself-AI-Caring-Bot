"""
Plan runtime tools — callable by Coach agent.

Each function opens its own DB session, delegates persisted decisions to the
authoritative lifecycle service, commits, and then reconciles explicit external
effects when the WP-02.2 contract owns them. Returns a plain dict result.

All DB / external imports are lazy (inside function bodies) so tests can stub
the authoritative lifecycle and reconciliation boundaries.

Tool registration in Coach prompt is T5.7.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

def _validate_hhmm(hhmm: str) -> None:
    """Reject malformed or impossible local wall-clock values."""
    from app.time_slots import TimeSlotError, canonicalize_hhmm

    try:
        canonicalize_hhmm(hhmm)
    except TimeSlotError as exc:
        raise ValueError(
            f"Invalid time format {hhmm!r} — expected HH:MM (e.g. '09:30')"
        ) from exc


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


# ─── Public tools ─────────────────────────────────────────────────────────────


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

    from app.db import SessionLocal  # lazy
    from app.lifecycle import (  # lazy
        LifecycleTransitionError,
        activate_plan,
        prepare_followup_evening_collection,
        recover_current_plan_activation,
    )
    from app.lifecycle_reconciliation import reconcile_scheduler_effects  # lazy

    with SessionLocal() as db:
        _, profile = _load_user_and_profile(db, user_id, lock=True)

        time_slots: dict = (profile.daily_time_slots or {}) if profile else {}
        day_time: Optional[str] = time_slots.get("DAY")
        if day_time is None:
            raise ValueError("saved_day_time_missing")

        evening_time: Optional[str] = None
        if plan_type == "MEDIUM":
            if not (profile and profile.evening_slot_collected):
                try:
                    pending = prepare_followup_evening_collection(
                        db,
                        user_id=user_id,
                        source_operation_id=source_operation_id,
                    )
                except LifecycleTransitionError as exc:
                    raise ValueError(str(exc)) from exc
                db.commit()
                return {
                    "status": "needs_evening_time",
                    "duplicate": pending.duplicate,
                    "disposition": "deferred",
                }
            evening_time = time_slots.get("EVENING")
            if evening_time is None:
                raise ValueError("saved_evening_time_missing")

        try:
            activation = activate_plan(
                db,
                user_id=user_id,
                plan_type=plan_type,
                day_time=day_time,
                evening_time=evening_time,
                source_operation_id=source_operation_id,
                require_plan_history=True,
                required_previous_status="abandoned",
            )
        except LifecycleTransitionError as exc:
            if str(exc) != "followup_activation_requires_no_active_plan":
                raise ValueError(str(exc)) from exc
            try:
                activation = recover_current_plan_activation(
                    db,
                    user_id=user_id,
                    plan_type=plan_type,
                    required_previous_status="abandoned",
                )
            except LifecycleTransitionError as recovery_exc:
                raise ValueError(str(recovery_exc)) from recovery_exc

        db.commit()

    activation = reconcile_scheduler_effects(activation)
    if not activation.external_effects_succeeded:
        return {
            "status": "error",
            "code": "activation_reconciliation_failed",
            "plan_id": activation.plan_id,
            "plan_type": activation.plan_type,
            "persisted": True,
            "jobs_reconciled": False,
            "duplicate": activation.duplicate,
            "disposition": "partial_failure",
        }

    logger.info(
        "[plan_runtime] create_followup_plan: user=%s plan_id=%s type=%s",
        user_id, activation.plan_id, activation.plan_type,
    )
    response = {
        "status": "ok",
        "plan_id": activation.plan_id,
        "plan_type": activation.plan_type,
        "jobs_reconciled": True,
        "duplicate": activation.duplicate,
        "disposition": "replayed" if activation.duplicate else "applied",
    }
    if activation.code == "recovered_existing_plan":
        response["recovered"] = True
    return response


def _finish_plan_format_result(result, *, pending_status: str) -> dict:
    from app.lifecycle_reconciliation import reconcile_scheduler_effects

    if result.code == "needs_evening_time":
        return {
            "status": pending_status,
            "target_plan_type": result.details.get("target_plan_type"),
            "duplicate": result.duplicate,
            "disposition": "deferred",
        }

    result = reconcile_scheduler_effects(result)
    if not result.external_effects_succeeded:
        return {
            "status": "error",
            "code": "switch_reconciliation_failed",
            "plan_id": result.plan_id,
            "source_plan_id": result.details.get("source_plan_id"),
            "plan_type": result.plan_type,
            "persisted": True,
            "duplicate": result.duplicate,
            "disposition": "partial_failure",
        }
    if result.code == "superseded":
        return {
            "status": "error",
            "code": "superseded",
            "plan_id": result.plan_id,
            "plan_type": result.plan_type,
            "duplicate": True,
            "disposition": "superseded",
            "current_disposition": result.details.get("current_disposition"),
        }
    return {
        "status": "ok",
        "plan_id": result.plan_id,
        "source_plan_id": result.details.get("source_plan_id"),
        "plan_type": result.plan_type,
        "jobs_reconciled": True,
        "duplicate": result.duplicate,
        "disposition": "replayed" if result.duplicate else "applied",
    }


def switch_plan_format(
    user_id: int,
    plan_type: str,
    *,
    source_operation_id: str,
) -> dict:
    """Atomically replace the current 7/14-day sequence with the other format."""
    normalized = str(plan_type).strip().upper()
    if normalized not in {"SHORT", "MEDIUM"}:
        raise ValueError(f"plan_type must be 'SHORT' or 'MEDIUM', got {plan_type!r}")

    from app.db import SessionLocal
    from app.lifecycle import LifecycleTransitionError, switch_plan_format as decide

    with SessionLocal() as db:
        try:
            result = decide(
                db,
                user_id=user_id,
                target_plan_type=normalized,
                source_operation_id=source_operation_id,
            )
        except LifecycleTransitionError as exc:
            raise ValueError(str(exc)) from exc
        db.commit()

    return _finish_plan_format_result(result, pending_status="needs_evening_time")


def recover_plan_format_switch(
    user_id: int,
    plan_type: str,
    *,
    source_operation_id: str,
) -> dict:
    """Resume only a durable switch intent or committed switch receipt."""
    normalized = str(plan_type).strip().upper()
    if normalized not in {"SHORT", "MEDIUM"}:
        raise ValueError(f"plan_type must be 'SHORT' or 'MEDIUM', got {plan_type!r}")

    from app.db import SessionLocal
    from app.lifecycle import (
        LifecycleTransitionError,
        recover_plan_format_switch as decide,
    )

    with SessionLocal() as db:
        try:
            result = decide(
                db,
                user_id=user_id,
                target_plan_type=normalized,
                source_operation_id=source_operation_id,
            )
        except LifecycleTransitionError as exc:
            raise ValueError(str(exc)) from exc
        db.commit()

    return _finish_plan_format_result(result, pending_status="not_ready")


def record_evening_time(
    user_id: int,
    hhmm: str,
    *,
    context: str,
    pending_source_operation_id: str,
    source_operation_id: str,
) -> dict:
    """Persist the user's chosen evening delivery time and mark slot as collected.

    Used before creating a MEDIUM plan for the first time.
    """
    _validate_hhmm(hhmm)

    from app.db import SessionLocal  # lazy
    from app.lifecycle import (  # lazy
        LifecycleTransitionError,
        record_evening_time_preference,
    )

    with SessionLocal() as db:
        try:
            result = record_evening_time_preference(
                db,
                user_id=user_id,
                hhmm=hhmm,
                context=context,
                pending_source_operation_id=pending_source_operation_id,
                source_operation_id=source_operation_id,
            )
        except LifecycleTransitionError as exc:
            raise ValueError(str(exc)) from exc
        db.commit()

    if result.code == "superseded":
        return {
            "status": "error",
            "code": "superseded",
            "evening_time": result.details.get("authoritative_value"),
            "requested_evening_time": hhmm,
            "saved": False,
            "applied": False,
            "duplicate": result.duplicate,
            "disposition": "superseded",
        }
    logger.info("[plan_runtime] record_evening_time: user=%s hhmm=%s", user_id, hhmm)
    return {
        "status": "ok",
        "evening_time": result.details.get(
            "authoritative_value", result.details.get("value")
        ),
        "saved": True,
        "applied": result.applied,
        "duplicate": result.duplicate,
        "disposition": "replayed" if result.duplicate else "applied",
    }


def change_day_time(
    user_id: int,
    hhmm: str,
    *,
    source_operation_id: str,
) -> dict:
    """Change the DAY slot delivery time and reschedule pending/delivered steps.

    Updates profile.daily_time_slots["DAY"], rewrites scheduled_for on all
    future pending steps via update_user_time_slots, then reschedules jobs.
    """
    _validate_hhmm(hhmm)

    from app.db import SessionLocal  # lazy
    from app.lifecycle import LifecycleTransitionError, change_delivery_time  # lazy
    from app.lifecycle_reconciliation import reconcile_scheduler_effects  # lazy

    with SessionLocal() as db:
        try:
            result = change_delivery_time(
                db,
                user_id=user_id,
                slot="DAY",
                hhmm=hhmm,
                source_operation_id=source_operation_id,
            )
        except LifecycleTransitionError as exc:
            raise ValueError(str(exc)) from exc
        db.commit()

    if result.code == "superseded":
        return {
            "status": "error",
            "code": "superseded",
            "day_time": result.details.get("authoritative_value"),
            "requested_day_time": hhmm,
            "saved": False,
            "jobs_reconciled": False,
            "duplicate": result.duplicate,
            "disposition": "superseded",
        }
    result = reconcile_scheduler_effects(result)
    effect = result.effects[0]
    if effect.state.value == "failed":
        return {
            "status": "error",
            "code": "schedule_reconciliation_failed",
            "day_time": hhmm,
            "saved": True,
            "jobs_reconciled": False,
            "duplicate": result.duplicate,
            "disposition": "partial_failure",
        }
    logger.info(
        "[plan_runtime] change_day_time: user=%s hhmm=%s rescheduled=%d",
        user_id, hhmm, effect.succeeded,
    )
    return {
        "status": "ok",
        "day_time": hhmm,
        "saved": True,
        "jobs_reconciled": effect.state.value,
        "rescheduled": effect.succeeded,
        "duplicate": result.duplicate,
        "disposition": (
            "deferred"
            if effect.state.value == "deferred"
            else "replayed"
            if result.duplicate
            else "applied"
        ),
    }


def change_evening_time(
    user_id: int,
    hhmm: str,
    *,
    source_operation_id: str,
) -> dict:
    """Change the EVENING slot delivery time and reschedule pending/delivered steps.

    Updates profile.daily_time_slots["EVENING"], rewrites scheduled_for on all
    future pending steps via update_user_time_slots, then reschedules jobs.
    """
    _validate_hhmm(hhmm)

    from app.db import SessionLocal  # lazy
    from app.lifecycle import LifecycleTransitionError, change_delivery_time  # lazy
    from app.lifecycle_reconciliation import reconcile_scheduler_effects  # lazy

    with SessionLocal() as db:
        try:
            result = change_delivery_time(
                db,
                user_id=user_id,
                slot="EVENING",
                hhmm=hhmm,
                source_operation_id=source_operation_id,
            )
        except LifecycleTransitionError as exc:
            raise ValueError(str(exc)) from exc
        db.commit()

    if result.code == "superseded":
        return {
            "status": "error",
            "code": "superseded",
            "evening_time": result.details.get("authoritative_value"),
            "requested_evening_time": hhmm,
            "saved": False,
            "jobs_reconciled": False,
            "duplicate": result.duplicate,
            "disposition": "superseded",
        }
    result = reconcile_scheduler_effects(result)
    effect = result.effects[0]
    if effect.state.value == "failed":
        return {
            "status": "error",
            "code": "schedule_reconciliation_failed",
            "evening_time": hhmm,
            "saved": True,
            "jobs_reconciled": False,
            "duplicate": result.duplicate,
            "disposition": "partial_failure",
        }
    logger.info(
        "[plan_runtime] change_evening_time: user=%s hhmm=%s rescheduled=%d",
        user_id, hhmm, effect.succeeded,
    )
    return {
        "status": "ok",
        "evening_time": hhmm,
        "saved": True,
        "jobs_reconciled": effect.state.value,
        "rescheduled": effect.succeeded,
        "duplicate": result.duplicate,
        "disposition": (
            "deferred"
            if effect.state.value == "deferred"
            else "replayed"
            if result.duplicate
            else "applied"
        ),
    }


def cancel_plan(user_id: int, *, source_operation_id: str) -> dict:
    """Atomically abandon the current plan and cancel its open steps."""
    from app.db import SessionLocal  # lazy
    from app.lifecycle import LifecycleTransitionError, abandon_current_plan  # lazy
    from app.lifecycle_reconciliation import reconcile_scheduler_effects  # lazy

    with SessionLocal() as db:
        try:
            result, _ = abandon_current_plan(
                db,
                user_id=user_id,
                source_operation_id=source_operation_id,
            )
        except LifecycleTransitionError as exc:
            raise ValueError(str(exc)) from exc
        db.commit()

    result = reconcile_scheduler_effects(result)
    if not result.external_effects_succeeded:
        return {
            "status": "error",
            "code": "cancel_reconciliation_failed",
            "plan_id": result.plan_id,
            "plan_status": result.status,
            "persisted": True,
            "jobs_reconciled": False,
            "duplicate": result.duplicate,
            "disposition": "partial_failure",
        }

    logger.info(
        "[plan_runtime] cancel_plan: user=%s external_effects=%d",
        user_id,
        sum(effect.succeeded for effect in result.effects),
    )
    total_days = 14 if result.plan_type == "MEDIUM" else 7
    return {
        "status": "ok",
        "plan_id": result.plan_id,
        "total_days": total_days,
        "jobs_reconciled": True,
        "duplicate": result.duplicate,
        "disposition": "replayed" if result.duplicate else "applied",
    }


def get_plan_status(user_id: int) -> dict:
    """Return the one derived current mode and current-plan summary."""
    from app.db import SessionLocal  # lazy
    from app.lifecycle import (  # lazy
        LifecycleInvariantError,
        LifecycleTransitionError,
        read_lifecycle_status,
    )

    with SessionLocal() as db:
        try:
            status = read_lifecycle_status(db, user_id)
        except (LifecycleInvariantError, LifecycleTransitionError) as exc:
            raise ValueError(str(exc)) from exc

    return status.to_runtime_payload()


def pause_plan(user_id: int, *, source_operation_id: str) -> dict:
    """Pause an active plan.

    Delegates to the plan-centric aggregate operation.
    """
    from app.db import SessionLocal  # lazy
    from app.lifecycle import LifecycleTransitionError, transition_current_plan  # lazy
    from app.lifecycle_reconciliation import reconcile_scheduler_effects  # lazy

    with SessionLocal() as db:
        try:
            result = transition_current_plan(
                db,
                user_id=user_id,
                operation="pause",
                source_operation_id=source_operation_id,
            )
        except LifecycleTransitionError as exc:
            raise ValueError(str(exc)) from exc
        db.commit()

    if result.code == "superseded":
        return {
            "status": "error",
            "code": "superseded",
            "plan_id": result.plan_id,
            "plan_status": result.status,
            "duplicate": True,
            "disposition": "superseded",
        }
    result = reconcile_scheduler_effects(result)
    if not result.external_effects_succeeded:
        return {
            "status": "error",
            "code": "pause_reconciliation_failed",
            "plan_id": result.plan_id,
            "plan_status": result.status,
            "persisted": True,
            "duplicate": result.duplicate,
            "disposition": "partial_failure",
        }

    logger.info("[plan_runtime] pause_plan: user=%s", user_id)
    return {
        "status": "ok",
        "plan_id": result.plan_id,
        "plan_status": result.status,
        "schedule_reconciliation": result.effects[0].state.value,
        "duplicate": result.duplicate,
        "disposition": "replayed" if result.duplicate else "applied",
    }


def resume_plan(user_id: int, *, source_operation_id: str) -> dict:
    """Resume a paused plan.

    Delegates to the plan-centric aggregate operation.
    """
    from app.db import SessionLocal  # lazy
    from app.lifecycle import LifecycleTransitionError, transition_current_plan  # lazy
    from app.lifecycle_reconciliation import reconcile_scheduler_effects  # lazy

    with SessionLocal() as db:
        try:
            result = transition_current_plan(
                db,
                user_id=user_id,
                operation="resume",
                source_operation_id=source_operation_id,
            )
        except LifecycleTransitionError as exc:
            raise ValueError(str(exc)) from exc
        db.commit()

    if result.code == "superseded":
        return {
            "status": "error",
            "code": "superseded",
            "plan_id": result.plan_id,
            "plan_status": result.status,
            "duplicate": True,
            "disposition": "superseded",
        }
    result = reconcile_scheduler_effects(result)
    if not result.external_effects_succeeded:
        return {
            "status": "error",
            "code": "resume_reconciliation_failed",
            "plan_id": result.plan_id,
            "plan_status": result.status,
            "persisted": True,
            "duplicate": result.duplicate,
            "disposition": "partial_failure",
        }

    logger.info("[plan_runtime] resume_plan: user=%s", user_id)
    return {
        "status": "ok",
        "plan_id": result.plan_id,
        "plan_status": result.status,
        "schedule_reconciliation": result.effects[0].state.value,
        "duplicate": result.duplicate,
        "disposition": "replayed" if result.duplicate else "applied",
    }

"""Plan creation service (T5.2).

Public API:
    create_plan(db, user_id, plan_type, day_time, evening_time) -> AIPlan

Reads active_days/work_days from user_profile internally.
No user-facing draft confirmation step — plan goes directly to ACTIVE.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.db import AIPlan, PlanDraftRecord, PlanDraftStep, UserProfile
from app.plan_drafts.plan_builder_v5 import PlanDraftV5, get_default_builder


# SHORT = 7 active days, MEDIUM = 14 active days
_PLAN_TYPE_TOTAL_DAYS = {
    "SHORT": 7,
    "MEDIUM": 14,
}


def create_plan(
    db: Session,
    user_id: int,
    plan_type: str,                 # "SHORT" | "MEDIUM"
    day_time: Optional[str] = None,  # "HH:MM"; if None, read from user_profile
    evening_time: Optional[str] = None,  # "HH:MM"; required for MEDIUM
) -> AIPlan:
    """Build, persist, and immediately finalize a plan for user_id.

    active_days / work_days are read from user_profile internally.
    orchestrator does not need to pass them.

    Raises:
        MissingEveningSlotError  if plan_type == "MEDIUM" and evening_time is None
        NoCandidatesError        if the library has no candidates for a slot
        FinalizationError        on DB or scheduling failure
    """
    from app.plan_finalization import finalize_plan

    from app.plan_drafts.plan_builder_v5 import MissingEveningSlotError

    profile = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()

    # Read time slots from profile if not provided explicitly
    time_slots: dict = (profile.daily_time_slots or {}) if profile else {}
    resolved_day_time = day_time or time_slots.get("DAY", "14:00")

    # MEDIUM requires an explicitly collected evening time.
    # UserProfile.daily_time_slots always has a default EVENING value ("21:00"),
    # so we must not fall back to it unless evening_slot_collected=True —
    # otherwise a MEDIUM plan silently uses the default instead of asking the user.
    if plan_type == "MEDIUM":
        if evening_time is not None:
            resolved_evening: Optional[str] = evening_time
        elif profile and profile.evening_slot_collected:
            resolved_evening = time_slots.get("EVENING")
        else:
            raise MissingEveningSlotError(
                "MEDIUM plan requires evening_time; "
                "evening_slot_collected is False — collect it before calling create_plan()"
            )
    else:
        resolved_evening = None

    builder = get_default_builder()
    draft_v5: PlanDraftV5 = builder.build(
        plan_type=plan_type,
        user_id=str(user_id),
        day_time=resolved_day_time,
        evening_time=resolved_evening,
    )

    draft_record = _persist_v5_draft(db, user_id, draft_v5)
    db.flush()

    plan: AIPlan = finalize_plan(
        db,
        user_id,
        draft_record,
        activation_time_utc=datetime.now(timezone.utc),
    )
    return plan


# ── Internal helpers ──────────────────────────────────────────────────────────

def _persist_v5_draft(
    db: Session,
    user_id: int,
    draft: PlanDraftV5,
) -> PlanDraftRecord:
    """Persist a PlanDraftV5 as a PlanDraftRecord + PlanDraftStep rows.

    focus / load are NULL for v5 plans (those concepts are removed).
    mechanic is stored per step.
    """
    total_days = _PLAN_TYPE_TOTAL_DAYS.get(draft.plan_type, len(draft.steps))

    record = PlanDraftRecord(
        id=uuid.UUID(draft.id),
        user_id=user_id,
        status="DRAFT",
        duration=draft.plan_type,   # "SHORT" | "MEDIUM"
        focus=None,                  # v5: no focus concept
        load=None,                   # v5: no load concept
        draft_data={
            "id": draft.id,
            "plan_type": draft.plan_type,
            "active_days_count": draft.active_days_count,
            "source_exercises": draft.source_exercises,
            "metadata": draft.metadata,
            "steps": [
                {
                    "step_id": s.step_id,
                    "day_number": s.day_number,
                    "time_slot": s.time_slot,
                    "mechanic": s.mechanic,
                    "exercise_id": s.exercise_id,
                }
                for s in draft.steps
            ],
        },
        total_days=total_days,
        total_steps=len(draft.steps),
        is_valid=True,
    )
    db.add(record)
    db.flush()

    for step in draft.steps:
        db.add(
            PlanDraftStep(
                draft_id=record.id,
                day_number=step.day_number,
                exercise_id=step.exercise_id,
                time_slot=step.time_slot,
                mechanic=step.mechanic,
                # Legacy columns — not used in v5, set to safe defaults
                slot_type="ACTION",
                category="",
                difficulty=None,
            )
        )

    return record


__all__ = ["create_plan"]

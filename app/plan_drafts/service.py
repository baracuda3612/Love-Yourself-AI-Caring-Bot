"""Plan draft creation and persistence helpers."""

from __future__ import annotations

from pathlib import Path
import uuid
from typing import Any, Dict

from sqlalchemy.orm import Session

from app.db import PlanDraftRecord, PlanDraftStep
from app.plan_drafts.draft_builder import (
    ContentLibrary,
    DraftBuilder,
    DraftValidationError,
    InsufficientLibraryError,
)
from app.plan_drafts.plan_types import Duration, Focus, Load, PlanDraft, PlanParameters, UserPolicy

CONTENT_LIBRARY_PATH = (
    Path(__file__).resolve().parents[2]
    / "resource"
    / "assets"
    / "content_library"
    / "tasks"
    / "burnout_combined_content_library.json"
)


def _build_plan_parameters(parameters: Dict[str, Any]) -> PlanParameters:
    duration = parameters.get("duration")
    focus = parameters.get("focus")
    load = parameters.get("load")
    preferred_time_slots = parameters.get("preferred_time_slots") or []

    focus_value = focus.lower() if isinstance(focus, str) else focus

    duration_value = None
    if duration:
        try:
            duration_value = Duration(duration)
        except ValueError:
            duration_value = None

    focus_enum = None
    if focus_value:
        try:
            focus_enum = Focus(focus_value)
        except ValueError:
            focus_enum = None

    load_value = None
    if load:
        try:
            load_value = Load(load)
        except ValueError:
            load_value = None

    user_policy = None
    if preferred_time_slots:
        user_policy = UserPolicy(preferred_time_slots=list(preferred_time_slots))

    return PlanParameters(
        duration=duration_value,
        focus=focus_enum,
        load=load_value,
        user_policy=user_policy,
    )


def _serialize_draft(draft: PlanDraft) -> Dict[str, Any]:
    return {
        "id": draft.id,
        "duration": draft.duration.value,
        "focus": draft.focus.value,
        "load": draft.load.value,
        "total_days": draft.total_days,
        "total_steps": len(draft.steps),
        "source_exercises": draft.source_exercises,
        "steps": [
            {
                "step_id": step.step_id,
                "day_number": step.day_number,
                "exercise_id": step.exercise_id,
                "exercise_name": step.exercise_name,
                "category": step.category,
                "impact_areas": step.impact_areas,
                "slot_type": step.slot_type.value,
                "time_slot": step.time_slot.value,
                "difficulty": step.difficulty,
                "energy_cost": step.energy_cost,
            }
            for step in draft.steps
        ],
        "is_valid": draft.is_valid(),
        "validation_errors": draft.validation_errors,
        "metadata": draft.metadata,
    }


def build_plan_draft(parameters: Dict[str, Any], user_id: str = "") -> PlanDraft:
    plan_parameters = _build_plan_parameters(parameters)
    try:
        # TODO: Replace file-based library loading with repository injection post-MVP.
        library = ContentLibrary(str(CONTENT_LIBRARY_PATH))
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise InsufficientLibraryError(str(exc)) from exc
    builder = DraftBuilder(library, user_id=user_id)
    return builder.build_plan_draft(plan_parameters)


def persist_plan_draft(db: Session, user_id: int, draft: PlanDraft) -> PlanDraftRecord:
    payload = _serialize_draft(draft)

    record = PlanDraftRecord(
        id=uuid.UUID(draft.id),
        user_id=user_id,
        status="DRAFT",
        duration=draft.duration.value,
        focus=draft.focus.value,
        load=draft.load.value,
        draft_data=payload,
        total_days=draft.total_days,
        total_steps=len(draft.steps),
        is_valid=draft.is_valid(),
    )
    db.add(record)

    for step in draft.steps:
        db.add(
            PlanDraftStep(
                draft_id=record.id,
                day_number=step.day_number,
                exercise_id=step.exercise_id,
                slot_type=step.slot_type.value,
                time_slot=step.time_slot.value,
                category=step.category,
                difficulty=step.difficulty,
            )
        )

    return record


def get_latest_draft(db: Session, user_id: int) -> PlanDraftRecord | None:
    return (
        db.query(PlanDraftRecord)
        .filter(PlanDraftRecord.user_id == user_id)
        .order_by(PlanDraftRecord.created_at.desc())
        .first()
    )


def delete_latest_draft(db: Session, user_id: int) -> PlanDraftRecord | None:
    draft = get_latest_draft(db, user_id)
    if draft is None:
        return None
    db.delete(draft)
    return draft


def overwrite_plan_draft(db: Session, user_id: int, draft: PlanDraft) -> PlanDraftRecord:
    existing = get_latest_draft(db, user_id)
    if existing is not None:
        db.delete(existing)
        db.flush()
    # TODO: enforce 1 draft per user at the DB level (unique constraint on user_id).
    return persist_plan_draft(db, user_id, draft)


__all__ = [
    "CONTENT_LIBRARY_PATH",
    "DraftValidationError",
    "InsufficientLibraryError",
    "build_plan_draft",
    "delete_latest_draft",
    "get_latest_draft",
    "overwrite_plan_draft",
    "persist_plan_draft",
]

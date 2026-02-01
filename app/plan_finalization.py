"""Plan draft finalization helpers (backend-only)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta, timezone

import logging
import pytz
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.db import (
    AIPlan,
    AIPlanDay,
    AIPlanStep,
    ContentLibrary,
    PlanDraftRecord,
    User,
)
from app.plan_activation.activation_anchor import resolve_activation_anchor_date
from app.schemas.planner import DifficultyLevel, StepType, PlanModule
from app.time_slots import normalize_time_slot
from app.telemetry import log_user_event
from app.scheduler import schedule_plan_step
from app.db import SessionLocal

logger = logging.getLogger(__name__)


class DraftNotFoundError(RuntimeError):
    """Raised when there is no draft to finalize."""


class InvalidDraftError(RuntimeError):
    """Raised when a draft is invalid or mismatched."""


class ActivePlanExistsError(RuntimeError):
    """Raised when an active plan already exists for the user."""


class FinalizationError(RuntimeError):
    """Raised when plan finalization fails."""


def validate_for_finalization(db: Session, user_id: int) -> PlanDraftRecord:
    draft = (
        db.query(PlanDraftRecord)
        .filter(PlanDraftRecord.user_id == user_id)
        .order_by(PlanDraftRecord.created_at.desc())
        .first()
    )
    if draft is None:
        raise DraftNotFoundError("draft_not_found")
    if draft.user_id != user_id:
        raise InvalidDraftError("draft_user_mismatch")
    if str(draft.status).upper() == "FINALIZED":
        raise InvalidDraftError("draft_already_finalized")
    if not draft.is_valid:
        raise InvalidDraftError("draft_invalid")
    active_plan = (
        db.query(AIPlan)
        .filter(AIPlan.user_id == user_id, AIPlan.status == "active")
        .first()
    )
    if active_plan is not None:
        raise ActivePlanExistsError("active_plan_exists")
    return draft


def _normalize_timezone(name: str | None) -> pytz.BaseTzInfo:
    try:
        return pytz.timezone(name or "UTC")
    except pytz.UnknownTimeZoneError:
        return pytz.UTC


_FIXED_TIME_SLOTS: dict[str, time] = {
    "MORNING": time(hour=9, minute=30),
    "DAY": time(hour=14, minute=0),
    "EVENING": time(hour=21, minute=0),
}


def _map_step_type(slot_type: str | None) -> StepType:
    if (slot_type or "").strip().upper() == "REST":
        return StepType.REST
    return StepType.ACTION


def _map_difficulty(difficulty: int | None) -> DifficultyLevel:
    value = difficulty or 1
    if value <= 2:
        return DifficultyLevel.EASY
    if value <= 4:
        return DifficultyLevel.MEDIUM
    return DifficultyLevel.HARD


def _build_step_title(content: ContentLibrary | None) -> str:
    if content and content.content_payload:
        title = content.content_payload.get("title")
        if title:
            return str(title)
    if content and content.internal_name:
        return str(content.internal_name)
    return "Завдання"


def _build_step_description(content: ContentLibrary | None) -> str:
    if not content or not content.content_payload:
        return ""
    payload = content.content_payload
    for key in ("description", "text", "instructions"):
        if payload.get(key):
            return str(payload[key])
    return ""


def _derive_plan_end_date(plan_start: datetime, total_days: int, tz: pytz.BaseTzInfo) -> datetime | None:
    if total_days <= 0:
        return None
    start_local = plan_start.astimezone(tz)
    end_local = start_local + timedelta(days=total_days)
    return end_local


def _resolve_time_slot(value: str) -> time:
    try:
        normalized = normalize_time_slot(value)
    except Exception as exc:
        raise FinalizationError("invalid_time_slot") from exc
    slot_time = _FIXED_TIME_SLOTS.get(normalized)
    if not slot_time:
        raise FinalizationError("invalid_time_slot")
    return slot_time


def _resolve_scheduled_for(
    *,
    anchor_date: datetime,
    day_number: int,
    time_slot: str,
    tz: pytz.BaseTzInfo,
) -> datetime:
    if day_number <= 0:
        raise FinalizationError("invalid_day_number")
    slot_time = _resolve_time_slot(time_slot)
    target_date = anchor_date.date() + timedelta(days=day_number - 1)
    naive = datetime.combine(target_date, slot_time)
    try:
        localized = tz.localize(naive)
    except pytz.NonExistentTimeError:
        localized = tz.localize(naive + timedelta(hours=1))
    except pytz.AmbiguousTimeError:
        localized = tz.localize(naive, is_dst=False)
    return localized.astimezone(timezone.utc)


def finalize_plan(
    db: Session,
    user_id: int,
    draft: PlanDraftRecord,
    *,
    activation_time_utc: datetime,
) -> AIPlan:
    try:
        with db.begin():
            user = (
                db.query(User)
                .filter(User.id == user_id)
                .with_for_update()
                .first()
            )
            if not user:
                raise FinalizationError("user_not_found")

            locked_draft = (
                db.query(PlanDraftRecord)
                .filter(PlanDraftRecord.id == draft.id)
                .with_for_update()
                .first()
            )
            if not locked_draft:
                raise FinalizationError("draft_missing")
            if locked_draft.user_id != user_id:
                raise InvalidDraftError("draft_user_mismatch")
            if str(locked_draft.status).upper() == "FINALIZED":
                raise InvalidDraftError("draft_already_finalized")
            if not locked_draft.is_valid:
                raise InvalidDraftError("draft_invalid")

            active_plan = (
                db.query(AIPlan)
                .filter(AIPlan.user_id == user_id, AIPlan.status == "active")
                .with_for_update()
                .first()
            )
            if active_plan is not None:
                raise ActivePlanExistsError("active_plan_exists")

            plan_start = resolve_activation_anchor_date(
                draft=locked_draft,
                activation_time_utc=activation_time_utc,
                user_timezone=user.timezone,
                slot_time_mapping=_FIXED_TIME_SLOTS,
            )
            plan = AIPlan(
                user_id=user_id,
                title="Personalized Recovery Plan",
                module_id=PlanModule.BURNOUT_RECOVERY,
                status="active",
                start_date=plan_start,
            )
            if hasattr(AIPlan, "activated_at"):
                plan.activated_at = plan_start
            if hasattr(AIPlan, "current_day"):
                plan.current_day = 1
            if hasattr(AIPlan, "duration"):
                plan.duration = locked_draft.duration
            if hasattr(AIPlan, "focus"):
                plan.focus = locked_draft.focus
            if hasattr(AIPlan, "load"):
                plan.load = locked_draft.load
            if hasattr(AIPlan, "total_days"):
                plan.total_days = locked_draft.total_days

            db.add(plan)
            db.flush()

            tz = _normalize_timezone(user.timezone)
            anchor_dt = plan_start.astimezone(tz)

            day_records: dict[int, AIPlanDay] = {}
            for day_number in range(1, locked_draft.total_days + 1):
                day_record = AIPlanDay(
                    plan_id=plan.id,
                    day_number=day_number,
                    focus_theme=None,
                )
                db.add(day_record)
                db.flush()
                day_records[day_number] = day_record

            step_rows = list(locked_draft.steps or [])
            if not step_rows:
                raise FinalizationError("draft_steps_missing")
            if locked_draft.total_steps and len(step_rows) < locked_draft.total_steps:
                raise FinalizationError("draft_steps_incomplete")

            exercise_ids = {str(step.exercise_id) for step in step_rows if step.exercise_id}
            content_entries = {
                content.id: content
                for content in db.query(ContentLibrary)
                .filter(ContentLibrary.id.in_(exercise_ids))
                .all()
            }
            if exercise_ids - set(content_entries.keys()):
                raise FinalizationError("content_library_missing")

            day_orders: dict[int, int] = defaultdict(int)
            for step_row in step_rows:
                day_number = int(step_row.day_number or 0)
                if day_number <= 0:
                    raise FinalizationError("invalid_day_number")
                day_record = day_records.get(day_number)
                if not day_record:
                    raise FinalizationError("day_not_found")
                exercise_id = str(step_row.exercise_id or "")
                content = content_entries.get(exercise_id)
                time_slot = normalize_time_slot(step_row.time_slot)
                scheduled_for = _resolve_scheduled_for(
                    anchor_date=anchor_dt,
                    day_number=day_number,
                    time_slot=time_slot,
                    tz=tz,
                )
                order_in_day = day_orders[day_number]
                day_orders[day_number] += 1
                db.add(
                    AIPlanStep(
                        day_id=day_record.id,
                        exercise_id=exercise_id,
                        title=_build_step_title(content),
                        description=_build_step_description(content),
                        step_type=_map_step_type(step_row.slot_type),
                        difficulty=_map_difficulty(step_row.difficulty),
                        order_in_day=order_in_day,
                        time_slot=time_slot,
                        scheduled_for=scheduled_for,
                    )
                )

            locked_draft.status = "FINALIZED"

            user.current_state = "ACTIVE"
            end_date = _derive_plan_end_date(plan_start, locked_draft.total_days, tz)
            user.plan_end_date = end_date
            if end_date is not None:
                plan.end_date = end_date

        return plan
    except (DraftNotFoundError, InvalidDraftError, ActivePlanExistsError):
        raise
    except (IntegrityError, ValueError) as exc:
        logger.error("Plan finalization failed for user %s: %s", user_id, exc)
        raise FinalizationError("transaction_failed") from exc


def activate_plan_side_effects(plan_id: int, user_id: int) -> None:
    try:
        with SessionLocal() as db:
            plan = (
                db.query(AIPlan)
                .options(selectinload(AIPlan.days).selectinload(AIPlanDay.steps), selectinload(AIPlan.user))
                .filter(AIPlan.id == plan_id, AIPlan.user_id == user_id)
                .first()
            )
            if not plan or not plan.user:
                logger.warning("Plan %s side effects skipped (missing plan/user).", plan_id)
                return
            for day in plan.days:
                for step in day.steps:
                    schedule_plan_step(step, plan.user)
            log_user_event(
                db,
                user_id=user_id,
                event_type="plan_activated",
                step_id=f"plan_{plan_id}",
                context={
                    "plan_id": str(plan_id),
                    "total_days": getattr(plan, "total_days", None),
                },
            )
            db.commit()
    except Exception as exc:
        logger.error("Plan activation side effects failed for plan %s: %s", plan_id, exc)

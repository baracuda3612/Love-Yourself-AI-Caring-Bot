"""Plan draft finalization helpers (backend-only)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

import logging
import pytz
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.db import (
    AIPlan,
    AIPlanDay,
    AIPlanStep,
    ContentLibrary,
    PlanDraftRecord,
    User,
    UserProfile,
)
from app.plan_activation.activation_anchor import resolve_activation_anchor_date
from app.schemas.planner import DifficultyLevel, StepType, PlanModule
from app.time_slots import (
    daily_time_slots_to_time_mapping,
    normalize_time_slot,
    resolve_daily_time_slots,
)
from app.active_days import (
    resolve_active_days,
    is_active_day,
    next_active_date,
    step_expires_at,
)
from app.telemetry import log_user_event
from app.scheduler import schedule_plan_step
from app.db import SessionLocal
from app.plan_duration import assert_canonical_total_days
from app.lifecycle import (
    LifecycleTransitionError,
    find_lifecycle_operation,
    mark_onboarding_completed,
    record_lifecycle_operation,
)

logger = logging.getLogger(__name__)


class DraftNotFoundError(RuntimeError):
    """Raised when there is no draft to finalize."""


class InvalidDraftError(RuntimeError):
    """Raised when a draft is invalid or mismatched."""


class ActivePlanExistsError(RuntimeError):
    """Raised when an active plan already exists for the user."""


class FinalizationError(RuntimeError):
    """Raised when plan finalization fails."""


@dataclass(frozen=True)
class PlanActivationResult:
    """Authoritative activation result, including retry disposition."""

    plan: AIPlan
    duplicate: bool


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
        .filter(AIPlan.user_id == user_id, AIPlan.status.in_(["active", "paused"]))
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


# _map_step_type and _map_difficulty removed in T5.2.
# v5 plans: step_type is always ACTION, difficulty is always EASY.


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


def _resolve_time_slot(value: str, slot_time_mapping: dict[str, time]) -> time:
    try:
        normalized = normalize_time_slot(value)
    except Exception as exc:
        raise FinalizationError("invalid_time_slot") from exc
    slot_time = slot_time_mapping.get(normalized)
    if not slot_time:
        raise FinalizationError("invalid_time_slot")
    return slot_time


def _resolve_scheduled_for(
    *,
    anchor_date: datetime,
    day_number: int,
    time_slot: str,
    tz: pytz.BaseTzInfo,
    slot_time_mapping: dict[str, time],
    real_date: date | None = None,
) -> datetime:
    if day_number <= 0:
        raise FinalizationError("invalid_day_number")
    slot_time = _resolve_time_slot(time_slot, slot_time_mapping)
    # real_date overrides the sequential day offset when active_days are used.
    target_date = real_date if real_date is not None else (anchor_date.date() + timedelta(days=day_number - 1))
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
    source_operation_id: str,
) -> PlanActivationResult:
    try:
        user = (
            db.query(User)
            .filter(User.id == user_id)
            .with_for_update()
            .first()
        )
        if not user:
            raise FinalizationError("user_not_found")

        existing_operation = find_lifecycle_operation(
            db,
            user_id,
            source_operation_id,
        )
        if existing_operation is not None:
            if existing_operation.operation != "activate":
                raise LifecycleTransitionError(
                    "source_operation_id already belongs to "
                    f"{existing_operation.operation}, not activate"
                )
            existing_plan = (
                db.query(AIPlan)
                .filter(
                    AIPlan.id == existing_operation.plan_id,
                    AIPlan.user_id == user_id,
                )
                .first()
            )
            if existing_plan is None:
                raise FinalizationError("activation_receipt_plan_missing")
            return PlanActivationResult(plan=existing_plan, duplicate=True)

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
            .filter(AIPlan.user_id == user_id, AIPlan.status.in_(["active", "paused"]))
            .with_for_update()
            .first()
        )
        if active_plan is not None:
            raise ActivePlanExistsError("active_plan_exists")

        profile = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
        slot_time_mapping = daily_time_slots_to_time_mapping(
            resolve_daily_time_slots(profile)
        )

        plan_start = resolve_activation_anchor_date(
            draft=locked_draft,
            activation_time_utc=activation_time_utc,
            user_timezone=user.timezone,
            slot_time_mapping=slot_time_mapping,
        )
        latest_cycle = (
            db.query(func.max(AIPlan.cycle_number))
            .filter(AIPlan.user_id == user_id)
            .scalar()
        )
        plan = AIPlan(
            user_id=user_id,
            title="Personalized Recovery Plan",
            module_id=PlanModule.BURNOUT_RECOVERY.value,
            status="active",
            cycle_number=int(latest_cycle or 0) + 1,
            activated_at=plan_start,
            version=1,
            start_date=plan_start,
        )
        if plan.module_id not in {
            PlanModule.BURNOUT_RECOVERY.value,
            PlanModule.SLEEP_OPTIMIZATION.value,
            PlanModule.DIGITAL_DETOX.value,
        }:
            raise FinalizationError("invalid_plan_module")
        if hasattr(AIPlan, "duration"):
            plan.duration = locked_draft.duration
        if hasattr(AIPlan, "focus"):
            plan.focus = locked_draft.focus
        if hasattr(AIPlan, "load"):
            plan.load = locked_draft.load
        if hasattr(AIPlan, "total_days"):
            try:
                assert_canonical_total_days(locked_draft.total_days)
            except ValueError as exc:
                raise FinalizationError("invalid_plan_duration") from exc
            plan.total_days = locked_draft.total_days

        # T5.2: plan.load is nullable for v5 plans — load concept removed.

        db.add(plan)
        db.flush()

        logger.info(
            "Plan %s activated with load=%s for user %s",
            plan.id,
            plan.load,
            user.id,
        )

        tz = _normalize_timezone(user.timezone)
        anchor_dt = plan_start.astimezone(tz)
        active_days = resolve_active_days(profile)

        # Build a mapping: logical day_number → real calendar date (active days only).
        # day_number 1 = first active date >= anchor, day_number N = Nth active date.
        anchor_date = anchor_dt.date()
        active_date_map: dict[int, date] = {}
        cursor = anchor_date
        for logical_day in range(1, locked_draft.total_days + 1):
            cursor = next_active_date(cursor, active_days)
            active_date_map[logical_day] = cursor
            cursor += timedelta(days=1)

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
        # T5.2: v5 exercise IDs may not be in DB content_library (sourced from JSON).
        # Log a warning instead of hard-failing.
        missing_from_db = exercise_ids - set(content_entries.keys())
        if missing_from_db:
            logger.warning(
                "content_library: %d exercises not in DB (v5 JSON source): %s",
                len(missing_from_db),
                missing_from_db,
            )

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

            # Use the real active calendar date for this logical day.
            real_date = active_date_map.get(day_number)
            if real_date is None:
                raise FinalizationError("active_date_missing")
            scheduled_for = _resolve_scheduled_for(
                anchor_date=anchor_dt,
                day_number=day_number,
                time_slot=time_slot,
                tz=tz,
                slot_time_mapping=slot_time_mapping,
                real_date=real_date,
            )
            expires = step_expires_at(scheduled_for, tz)

            # T5.2: v5 plans have no REST slots and no difficulty tiers.
            step_type = StepType.ACTION.value
            difficulty = DifficultyLevel.EASY.value
            # mechanic is snapshotted at build time — never recomputed (invariant 6, T5.1).
            mechanic = getattr(step_row, "mechanic", None)
            if mechanic is None:
                # Legacy row pre-T5.2 — acceptable fallback.
                # If this appears in logs for NEW plans, _persist_v5_draft is not writing mechanic correctly.
                logger.debug(
                    "mechanic not set on step %s — legacy row, defaulting to switch", step_row.id
                )
                mechanic = "switch"
            order_in_day = day_orders[day_number]
            day_orders[day_number] += 1
            db.add(
                AIPlanStep(
                    day_id=day_record.id,
                    exercise_id=exercise_id,
                    title=_build_step_title(content),
                    description=_build_step_description(content),
                    step_type=step_type,
                    difficulty=difficulty,
                    mechanic=mechanic,
                    order_in_day=order_in_day,
                    time_slot=time_slot,
                    scheduled_for=scheduled_for,
                    expires_at=expires,
                    step_status="pending",
                )
            )
        locked_draft.status = "FINALIZED"

        # A finalized plan proves onboarding completion. No legacy user FSM or
        # scheduled end-date mirror is written; completion is derived from the
        # authoritative terminal step facts.
        mark_onboarding_completed(db, user_id)
        record_lifecycle_operation(
            db,
            user_id=user_id,
            plan_id=plan.id,
            source_operation_id=source_operation_id,
            operation="activate",
            result_status="active",
        )
        db.flush()

        return PlanActivationResult(plan=plan, duplicate=False)
    except (DraftNotFoundError, InvalidDraftError, ActivePlanExistsError):
        raise
    except (IntegrityError, LifecycleTransitionError, ValueError) as exc:
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

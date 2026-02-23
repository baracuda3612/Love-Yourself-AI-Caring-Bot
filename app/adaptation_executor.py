from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session, selectinload

from app.adaptation_types import AdaptationIntent, AdaptationNotEligibleError
from app.db import AIPlan, AIPlanDay, AIPlanStep, AIPlanVersion, User
from app.plan_adaptations import apply_plan_adaptation
from app.scheduler import cancel_plan_step_jobs
from app.telemetry import log_user_event
from app.time_slots import compute_scheduled_for, resolve_daily_time_slots

logger = logging.getLogger(__name__)


class AdaptationExecutor:
    """Backend executor for plan adaptations.

    Handles plan modification based on adaptation intent.
    Implementation status: Skeleton only (Phase 3: TASK-3.x).
    """

    def execute(
        self,
        db: Session,
        plan_id: int,
        intent: AdaptationIntent,
        params: dict | None = None,
    ) -> list[int]:
        """Execute adaptation on a plan.

        Returns list of step_ids to reschedule AFTER caller commits the DB session.
        Caller MUST call reschedule_plan_steps(returned_ids) after commit.
        Empty list means no rescheduling needed.
        """
        if intent == AdaptationIntent.PAUSE_PLAN:
            return self._pause_plan(db, plan_id)
        elif intent == AdaptationIntent.RESUME_PLAN:
            return self._resume_plan(db, plan_id)
        elif intent == AdaptationIntent.REDUCE_DAILY_LOAD:
            self._reduce_daily_load(db, plan_id, params)
        elif intent == AdaptationIntent.INCREASE_DAILY_LOAD:
            return self._increase_daily_load(db, plan_id, params)
        elif intent == AdaptationIntent.LOWER_DIFFICULTY:
            self._lower_difficulty(db, plan_id)
        elif intent == AdaptationIntent.INCREASE_DIFFICULTY:
            self._increase_difficulty(db, plan_id)
        elif intent == AdaptationIntent.EXTEND_PLAN_DURATION:
            return self._extend_plan_duration(db, plan_id, params)
        elif intent == AdaptationIntent.SHORTEN_PLAN_DURATION:
            return self._shorten_plan_duration(db, plan_id, params)
        elif intent == AdaptationIntent.CHANGE_MAIN_CATEGORY:
            return self._change_main_category(db, plan_id, params)
        else:
            raise ValueError(f"Unknown adaptation intent: {intent}")
        return []

    def _reduce_daily_load(self, db: Session, plan_id: int, params: dict | None = None) -> None:
        from app.plan_adaptations import _iter_future_steps

        plan = (
            db.query(AIPlan)
            .options(selectinload(AIPlan.days).selectinload(AIPlanDay.steps))
            .filter(AIPlan.id == plan_id)
            .first()
        )
        if not plan:
            raise AdaptationNotEligibleError("plan_not_found")
        if plan.status != "active":
            raise AdaptationNotEligibleError("plan_not_active")

        current_slots = list(plan.preferred_time_slots or [])
        if len(current_slots) <= 1:
            raise AdaptationNotEligibleError("already_at_minimum_load")

        slot_to_remove = (params or {}).get("slot_to_remove", "").upper()
        if slot_to_remove not in {"MORNING", "DAY", "EVENING"}:
            raise AdaptationNotEligibleError("slot_to_remove_missing_or_invalid")
        if slot_to_remove not in current_slots:
            raise AdaptationNotEligibleError("slot_not_in_plan")

        effective_from = datetime.now(timezone.utc)

        canceled_ids: list[int] = []
        for _day, step in _iter_future_steps(plan, effective_from):
            if step.time_slot == slot_to_remove:
                step.canceled_by_adaptation = True
                step.scheduled_for = None
                canceled_ids.append(step.id)

        if not canceled_ids:
            raise AdaptationNotEligibleError("no_future_steps_in_slot")

        new_slots = self._canonical_slots([s for s in current_slots if s != slot_to_remove])
        plan.preferred_time_slots = new_slots
        plan.load = self._slots_to_load(len(new_slots))

        db.add(
            AIPlanVersion(
                plan_id=plan_id,
                applied_adaptation_type="REDUCE_DAILY_LOAD",
                diff={
                    "effective_from": effective_from.isoformat(),
                    "slot_removed": slot_to_remove,
                    "canceled_step_ids": canceled_ids,
                    "new_load": plan.load,
                    "new_slots": new_slots,
                },
            )
        )

        log_user_event(
            db=db,
            user_id=plan.user_id,
            event_type="plan_adapted",
            context={
                "plan_id": plan_id,
                "adaptation_type": "REDUCE_DAILY_LOAD",
                "slot_removed": slot_to_remove,
                "canceled_step_count": len(canceled_ids),
                "new_load": plan.load,
            },
        )

        if canceled_ids:
            cancel_plan_step_jobs(canceled_ids)

    def _increase_daily_load(self, db: Session, plan_id: int, params: dict | None = None) -> list[int]:
        from app.plan_adaptations import _iter_future_steps
        from app.db import ContentLibrary as ContentLibraryModel

        plan = (
            db.query(AIPlan)
            .options(selectinload(AIPlan.days).selectinload(AIPlanDay.steps))
            .filter(AIPlan.id == plan_id)
            .first()
        )
        if not plan:
            raise AdaptationNotEligibleError("plan_not_found")
        if plan.status != "active":
            raise AdaptationNotEligibleError("plan_not_active")

        current_slots = list(plan.preferred_time_slots or [])
        if len(current_slots) >= 3:
            raise AdaptationNotEligibleError("already_at_maximum_load")

        all_slots = ["MORNING", "DAY", "EVENING"]
        available_slots = [s for s in all_slots if s not in current_slots]

        # MID→INTENSIVE: автоматично — єдиний можливий слот
        if len(current_slots) == 2:
            slot_to_add = available_slots[0]
        else:
            # LITE→MID: explicit вибір з params
            slot_to_add = (params or {}).get("slot_to_add", "").upper()
            if slot_to_add not in available_slots:
                raise AdaptationNotEligibleError("slot_to_add_missing_or_invalid")

        effective_from = datetime.now(timezone.utc)
        difficulty_map = {"EASY": 1, "MEDIUM": 2, "HARD": 3}

        added_ids: list[int] = []
        for day in plan.days:
            future_in_day = [
                step
                for step in day.steps
                if not step.is_completed and not step.skipped and not step.canceled_by_adaptation
            ]
            if not future_in_day:
                continue

            ref_step = future_in_day[0]
            ref_difficulty_int = difficulty_map.get(str(ref_step.difficulty).upper(), 1)
            existing_ids = {s.exercise_id for s in future_in_day}

            candidate = (
                db.query(ContentLibraryModel)
                .filter(
                    ContentLibraryModel.category == (plan.focus or "").lower(),
                    ContentLibraryModel.difficulty <= ref_difficulty_int,
                    ContentLibraryModel.is_active == True,
                    ContentLibraryModel.id.notin_(existing_ids),
                )
                .order_by(ContentLibraryModel.id)
                .first()
            )
            if not candidate:
                candidate = (
                    db.query(ContentLibraryModel)
                    .filter(
                        ContentLibraryModel.is_active == True,
                        ContentLibraryModel.id.notin_(existing_ids),
                    )
                    .order_by(ContentLibraryModel.id)
                    .first()
                )
            if not candidate:
                continue

            new_step = AIPlanStep(
                day_id=day.id,
                exercise_id=candidate.id,
                title="",
                order_in_day=len(future_in_day) + 1,
                time_slot=slot_to_add,
                slot_type="SUPPORT",  # TODO: rebalancing — TASK-3.x-rebalance
                difficulty=ref_step.difficulty,
                scheduled_for=None,
            )
            db.add(new_step)
            db.flush()
            added_ids.append(new_step.id)

        if not added_ids:
            raise AdaptationNotEligibleError("no_future_days_to_add_steps")

        new_slots = self._canonical_slots(current_slots + [slot_to_add])
        plan.preferred_time_slots = new_slots
        plan.load = self._slots_to_load(len(new_slots))

        db.add(
            AIPlanVersion(
                plan_id=plan_id,
                applied_adaptation_type="INCREASE_DAILY_LOAD",
                diff={
                    "effective_from": effective_from.isoformat(),
                    "slot_added": slot_to_add,
                    "added_step_ids": added_ids,
                    "new_load": plan.load,
                    "new_slots": new_slots,
                },
            )
        )

        log_user_event(
            db=db,
            user_id=plan.user_id,
            event_type="plan_adapted",
            context={
                "plan_id": plan_id,
                "adaptation_type": "INCREASE_DAILY_LOAD",
                "slot_added": slot_to_add,
                "added_step_count": len(added_ids),
                "new_load": plan.load,
            },
        )

        # Повертаємо added_ids для post-commit reschedule в orchestrator.
        # Scheduler перевіряє plan.status == "active" тому reschedule має відбутись після commit.
        return added_ids

    def _lower_difficulty(self, db: Session, plan_id: int) -> None:
        """Lower difficulty by 1 level (TASK-3.5)."""
        raise NotImplementedError("LOWER_DIFFICULTY implementation in TASK-3.5")

    def _increase_difficulty(self, db: Session, plan_id: int) -> None:
        """Increase difficulty by 1 level (TASK-3.5)."""
        raise NotImplementedError("INCREASE_DIFFICULTY implementation in TASK-3.5")

    def _extend_plan_duration(self, db: Session, plan_id: int, params: dict | None) -> list[int]:
        from app.plan_drafts.service import build_plan_draft

        plan = (
            db.query(AIPlan)
            .options(selectinload(AIPlan.days).selectinload(AIPlanDay.steps))
            .filter(AIPlan.id == plan_id)
            .first()
        )
        if not plan:
            raise AdaptationNotEligibleError("plan_not_found")
        if plan.status != "active":
            raise AdaptationNotEligibleError("plan_not_active")

        target_days = int((params or {}).get("target_duration", 0))
        if target_days not in {14, 21, 90}:
            raise AdaptationNotEligibleError("invalid_target_duration")

        current_total = plan.total_days or 0
        if target_days <= current_total:
            raise AdaptationNotEligibleError("target_not_greater_than_current")

        user = db.query(User).filter(User.id == plan.user_id).first()
        if not user:
            raise AdaptationNotEligibleError("user_not_found")
        if not plan.start_date:
            raise AdaptationNotEligibleError("plan_has_no_start_date")

        # Duration mapping для DraftBuilder
        target_duration_map = {14: "SHORT", 21: "STANDARD", 90: "LONG"}
        draft_duration = target_duration_map[target_days]

        slot_strings = plan.preferred_time_slots or []

        params_dict = {
            "duration": draft_duration,
            "focus": plan.focus or "somatic",
            "load": plan.load,
            "preferred_time_slots": slot_strings,
        }

        # Генеруємо повний план для target_days.
        # TODO: TASK-extend-cooldown — pass exercise_last_used from existing plan steps
        # into DraftBuilder so cooldown tracking is continuous across old and new days.
        # Current behavior: new days may repeat exercises from last ~cooldown_days of existing plan.
        draft = build_plan_draft(params_dict)

        # Беремо тільки нові дні — після current_total
        new_steps = [s for s in draft.steps if s.day_number > current_total]
        if not new_steps:
            raise AdaptationNotEligibleError("draft_builder_returned_no_new_days")

        daily_time_slots = resolve_daily_time_slots(user.profile)
        plan_start = plan.start_date

        # Групуємо по day_number
        days_dict: dict[int, list] = {}
        for step in new_steps:
            days_dict.setdefault(step.day_number, []).append(step)

        added_step_ids: list[int] = []

        for day_number in sorted(days_dict.keys()):
            day_steps = days_dict[day_number]

            day_record = AIPlanDay(
                plan_id=plan_id,
                day_number=day_number,
                focus_theme=plan.focus or "",
            )
            db.add(day_record)
            db.flush()

            for index, step in enumerate(day_steps):
                scheduled_for = compute_scheduled_for(
                    plan_start=plan_start,
                    day_number=day_number,
                    time_slot=step.time_slot.value if hasattr(step.time_slot, "value") else str(step.time_slot),
                    timezone_name=user.timezone,
                    daily_time_slots=daily_time_slots,
                )
                new_step = AIPlanStep(
                    day_id=day_record.id,
                    exercise_id=step.exercise_id,
                    title=step.exercise_name or "",
                    order_in_day=index,
                    time_slot=step.time_slot.value if hasattr(step.time_slot, "value") else str(step.time_slot),
                    slot_type=step.slot_type.value if hasattr(step.slot_type, "value") else str(step.slot_type),
                    difficulty=self._int_to_difficulty(step.difficulty),
                    scheduled_for=scheduled_for,
                    canceled_by_adaptation=False,
                )
                db.add(new_step)
                db.flush()
                added_step_ids.append(new_step.id)

        old_total = plan.total_days
        plan.total_days = target_days

        # end_date offset: day_number=N maps to start_date + (N-1).
        # end_date = start_date + total_days is intentional +1 over last task day.
        # Consistent with _derive_plan_end_date in orchestrator.
        if plan.start_date:
            from datetime import timedelta

            plan.end_date = plan.start_date + timedelta(days=target_days)
            user.plan_end_date = plan.end_date

        db.add(
            AIPlanVersion(
                plan_id=plan_id,
                applied_adaptation_type="EXTEND_PLAN_DURATION",
                diff={
                    "old_total_days": old_total,
                    "new_total_days": target_days,
                    "days_added": target_days - old_total,
                    "extended_from_day": plan.current_day or 1,
                    "added_step_ids": added_step_ids,
                },
            )
        )

        log_user_event(
            db=db,
            user_id=plan.user_id,
            event_type="plan_adapted",
            context={
                "plan_id": plan_id,
                "adaptation_type": "EXTEND_PLAN_DURATION",
                "old_total_days": old_total,
                "new_total_days": target_days,
                "added_step_count": len(added_step_ids),
            },
        )

        return added_step_ids

    def _shorten_plan_duration(self, db: Session, plan_id: int, params: dict | None) -> list[int]:
        from app.plan_adaptations import _iter_future_steps

        plan = (
            db.query(AIPlan)
            .options(selectinload(AIPlan.days).selectinload(AIPlanDay.steps))
            .filter(AIPlan.id == plan_id)
            .first()
        )
        if not plan:
            raise AdaptationNotEligibleError("plan_not_found")
        if plan.status != "active":
            raise AdaptationNotEligibleError("plan_not_active")

        target_days = int((params or {}).get("target_duration", 0))
        if target_days not in {7, 14, 21}:
            raise AdaptationNotEligibleError("invalid_target_duration")

        current_total = plan.total_days or 0
        if target_days >= current_total:
            raise AdaptationNotEligibleError("target_not_less_than_current")

        current_day = plan.current_day or 1
        if current_day > target_days:
            raise AdaptationNotEligibleError("current_day_exceeds_target")

        user = db.query(User).filter(User.id == plan.user_id).first()
        if not user:
            raise AdaptationNotEligibleError("user_not_found")

        effective_from = datetime.now(timezone.utc)

        # Скасовуємо тільки future steps на днях > target_days.
        # Completed days за межею не чіпаємо — вони historical record.
        canceled_ids: list[int] = []
        for day, step in _iter_future_steps(plan, effective_from):
            if day.day_number > target_days:
                step.canceled_by_adaptation = True
                step.scheduled_for = None
                canceled_ids.append(step.id)

        old_total = plan.total_days
        plan.total_days = target_days

        # Consistent offset — see _derive_plan_end_date in orchestrator
        if plan.start_date:
            from datetime import timedelta

            plan.end_date = plan.start_date + timedelta(days=target_days)
            user.plan_end_date = plan.end_date

        db.add(
            AIPlanVersion(
                plan_id=plan_id,
                applied_adaptation_type="SHORTEN_PLAN_DURATION",
                diff={
                    "old_total_days": old_total,
                    "new_total_days": target_days,
                    "days_removed": old_total - target_days,
                    "shortened_from_day": current_day,
                    "canceled_step_ids": canceled_ids,
                },
            )
        )

        log_user_event(
            db=db,
            user_id=plan.user_id,
            event_type="plan_adapted",
            context={
                "plan_id": plan_id,
                "adaptation_type": "SHORTEN_PLAN_DURATION",
                "old_total_days": old_total,
                "new_total_days": target_days,
                "canceled_step_count": len(canceled_ids),
            },
        )

        if canceled_ids:
            cancel_plan_step_jobs(canceled_ids)

        return []

    def _pause_plan(self, db: Session, plan_id: int) -> list[int]:
        plan = (
            db.query(AIPlan)
            .options(selectinload(AIPlan.days).selectinload(AIPlanDay.steps))
            .filter(AIPlan.id == plan_id)
            .first()
        )
        if not plan:
            raise AdaptationNotEligibleError("plan_not_found")
        if plan.status == "paused":
            raise AdaptationNotEligibleError("already_paused")
        if plan.status != "active":
            raise AdaptationNotEligibleError("plan_not_active")

        result = apply_plan_adaptation(
            db=db,
            plan_id=plan_id,
            adaptation_payload={
                "adaptation_type": "pause",
                "effective_from": datetime.now(timezone.utc),
                "params": {},
            },
        )

        plan.status = "paused"

        log_user_event(
            db=db,
            user_id=plan.user_id,
            event_type="plan_paused",
            context={
                "plan_id": plan_id,
                "canceled_step_count": len(result.canceled_step_ids),
            },
        )

        if result.canceled_step_ids:
            # cancel is idempotent and safe to call before commit.
            # If commit later fails, scheduler reconciliation on restart
            # will restore jobs for active plans.
            cancel_plan_step_jobs(result.canceled_step_ids)

        return []

    def _resume_plan(self, db: Session, plan_id: int) -> list[int]:
        plan = (
            db.query(AIPlan)
            .options(selectinload(AIPlan.days).selectinload(AIPlanDay.steps))
            .filter(AIPlan.id == plan_id)
            .first()
        )
        if not plan:
            raise AdaptationNotEligibleError("plan_not_found")
        if plan.status != "paused":
            raise AdaptationNotEligibleError("not_paused")

        result = apply_plan_adaptation(
            db=db,
            plan_id=plan_id,
            adaptation_payload={
                "adaptation_type": "resume",
                "effective_from": datetime.now(timezone.utc),
                "params": {},
            },
        )

        plan.status = "active"

        log_user_event(
            db=db,
            user_id=plan.user_id,
            event_type="plan_resumed",
            context={
                "plan_id": plan_id,
                "rescheduled_step_count": len(result.rescheduled_step_ids),
            },
        )

        return result.rescheduled_step_ids

    @staticmethod
    def _slots_to_load(slot_count: int) -> str:
        """Map slot count to canonical load value."""
        return {1: "LITE", 2: "MID", 3: "INTENSIVE"}.get(slot_count, "LITE")

    @staticmethod
    def _canonical_slots(slots: list[str]) -> list[str]:
        """Return slots in canonical MORNING → DAY → EVENING order."""
        order = ["MORNING", "DAY", "EVENING"]
        return [s for s in order if s in slots]

    @staticmethod
    def _int_to_difficulty(value) -> str:
        """Convert DraftBuilder int difficulty (1/2/3) to AIPlanStep enum string."""
        mapping = {1: "EASY", 2: "MEDIUM", 3: "HARD"}
        try:
            return mapping.get(int(value), "EASY")
        except (TypeError, ValueError):
            return str(value) if value else "EASY"

    def _change_main_category(self, db: Session, plan_id: int, params: dict | None) -> list[int]:
        """Change main focus category by pausing old plan and creating new active plan."""
        from app.plan_adaptations import _iter_future_steps
        from app.plan_drafts.draft_builder import DraftValidationError
        from app.plan_drafts.service import InsufficientLibraryError, build_plan_draft

        plan = (
            db.query(AIPlan)
            .options(selectinload(AIPlan.days).selectinload(AIPlanDay.steps))
            .filter(AIPlan.id == plan_id)
            .first()
        )
        if not plan:
            raise AdaptationNotEligibleError("plan_not_found")
        if plan.status != "active":
            raise AdaptationNotEligibleError("plan_not_active")

        user = db.query(User).filter(User.id == plan.user_id).first()
        if not user:
            raise AdaptationNotEligibleError("user_not_found")

        target_category = ((params or {}).get("target_category") or "").lower().strip()
        allowed_categories = {"somatic", "cognitive", "boundaries", "rest", "mixed"}
        if target_category not in allowed_categories:
            raise AdaptationNotEligibleError("invalid_target_category")
        if target_category == (plan.focus or "").lower():
            raise AdaptationNotEligibleError("same_category_as_current")
        if not plan.preferred_time_slots:
            raise AdaptationNotEligibleError("plan_has_no_time_slots")

        other_active = (
            db.query(AIPlan)
            .filter(AIPlan.user_id == plan.user_id, AIPlan.status == "active", AIPlan.id != plan_id)
            .first()
        )
        if other_active:
            raise AdaptationNotEligibleError("another_active_plan_exists")

        days_to_duration = {7: "SHORT", 14: "SHORT", 21: "STANDARD", 90: "LONG"}
        draft_duration = days_to_duration.get(plan.total_days)
        if draft_duration is None:
            logger.warning(
                "[CHANGE_MAIN_CATEGORY] Unexpected total_days=%s for plan %s, fallback to STANDARD",
                plan.total_days,
                plan_id,
            )
            draft_duration = "STANDARD"

        params_dict = {
            "duration": draft_duration,
            "focus": target_category,
            "load": plan.load,
            "preferred_time_slots": list(plan.preferred_time_slots),
        }
        try:
            draft = build_plan_draft(params_dict, user_id=str(plan.user_id))
        except (InsufficientLibraryError, DraftValidationError) as exc:
            raise AdaptationNotEligibleError("content_library_insufficient") from exc

        effective_from = datetime.now(timezone.utc)
        canceled_ids: list[int] = []
        for _day, step in _iter_future_steps(plan, effective_from):
            step.canceled_by_adaptation = True
            step.scheduled_for = None
            canceled_ids.append(step.id)

        old_focus = plan.focus
        plan.status = "paused"

        new_total_days = draft.total_days
        new_start = datetime.now(timezone.utc)
        new_end = new_start + timedelta(days=new_total_days)

        new_plan_kwargs = {
            "user_id": plan.user_id,
            "title": f"Plan: {target_category}",
            "status": "active",
            "focus": target_category,
            "load": plan.load,
            "preferred_time_slots": list(plan.preferred_time_slots),
            "total_days": new_total_days,
            "start_date": new_start,
            "end_date": new_end,
            "current_day": 1,
            "adaptation_version": 1,
        }
        if hasattr(plan, "module_id"):
            new_plan_kwargs["module_id"] = plan.module_id

        new_plan = AIPlan(**new_plan_kwargs)
        db.add(new_plan)
        db.flush()

        user.plan_end_date = new_end

        daily_time_slots = resolve_daily_time_slots(user.profile)
        added_step_ids: list[int] = []

        days_dict: dict[int, list] = {}
        for step in draft.steps:
            days_dict.setdefault(step.day_number, []).append(step)

        for day_number in sorted(days_dict.keys()):
            day_steps = days_dict[day_number]

            day_record = AIPlanDay(
                plan_id=new_plan.id,
                day_number=day_number,
                focus_theme=target_category,
            )
            db.add(day_record)
            db.flush()

            for index, step in enumerate(day_steps):
                time_slot_str = step.time_slot.value if hasattr(step.time_slot, "value") else str(step.time_slot)
                slot_type_str = step.slot_type.value if hasattr(step.slot_type, "value") else str(step.slot_type)

                scheduled_for = compute_scheduled_for(
                    plan_start=new_start,
                    day_number=day_number,
                    time_slot=time_slot_str,
                    timezone_name=user.timezone,
                    daily_time_slots=daily_time_slots,
                )

                new_step = AIPlanStep(
                    day_id=day_record.id,
                    exercise_id=step.exercise_id,
                    title=step.exercise_name or "",
                    order_in_day=index,
                    time_slot=time_slot_str,
                    slot_type=slot_type_str,
                    difficulty=self._int_to_difficulty(step.difficulty),
                    scheduled_for=scheduled_for,
                    canceled_by_adaptation=False,
                )
                db.add(new_step)
                db.flush()
                added_step_ids.append(new_step.id)

        db.add(
            AIPlanVersion(
                plan_id=plan_id,
                applied_adaptation_type="CHANGE_MAIN_CATEGORY",
                diff={
                    "old_focus": old_focus,
                    "new_focus": target_category,
                    "new_plan_id": new_plan.id,
                    "canceled_step_ids": canceled_ids,
                    "canceled_step_count": len(canceled_ids),
                    "added_step_count": len(added_step_ids),
                },
            )
        )

        log_user_event(
            db=db,
            user_id=plan.user_id,
            event_type="plan_adapted",
            context={
                "plan_id": plan_id,
                "adaptation_type": "CHANGE_MAIN_CATEGORY",
                "old_focus": old_focus,
                "new_focus": target_category,
                "new_plan_id": new_plan.id,
                "canceled_step_count": len(canceled_ids),
                "added_step_count": len(added_step_ids),
            },
        )

        if canceled_ids:
            cancel_plan_step_jobs(canceled_ids)

        return added_step_ids

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session, selectinload

from app.adaptation_types import AdaptationIntent, AdaptationNotEligibleError
from app.db import AIPlan, AIPlanDay, AIPlanStep, AIPlanVersion
from app.plan_adaptations import apply_plan_adaptation
from app.scheduler import cancel_plan_step_jobs
from app.telemetry import log_user_event

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
            self._extend_plan_duration(db, plan_id, params)
        elif intent == AdaptationIntent.SHORTEN_PLAN_DURATION:
            self._shorten_plan_duration(db, plan_id, params)
        elif intent == AdaptationIntent.CHANGE_MAIN_CATEGORY:
            self._change_main_category(db, plan_id, params)
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

    def _extend_plan_duration(self, db: Session, plan_id: int, params: dict | None) -> None:
        """Extend plan duration (TASK-3.4)."""
        raise NotImplementedError("EXTEND_PLAN_DURATION implementation in TASK-3.4")

    def _shorten_plan_duration(self, db: Session, plan_id: int, params: dict | None) -> None:
        """Shorten plan duration (TASK-3.4)."""
        raise NotImplementedError("SHORTEN_PLAN_DURATION implementation in TASK-3.4")

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

    def _change_main_category(self, db: Session, plan_id: int, params: dict | None) -> None:
        """Change main focus category (TASK-3.6)."""
        raise NotImplementedError("CHANGE_MAIN_CATEGORY implementation in TASK-3.6")

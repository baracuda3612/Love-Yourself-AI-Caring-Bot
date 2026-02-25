from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session, selectinload

from app.adaptation_types import AdaptationIntent, AdaptationNotEligibleError, get_adaptation_category
from app.db import AIPlan, AIPlanDay, AIPlanStep, AIPlanVersion, AdaptationHistory, User
from app.plan_adaptations import apply_plan_adaptation
from app.telemetry import log_user_event
from app.time_slots import compute_scheduled_for, resolve_daily_time_slots
from app.plan_duration import DAYS_TO_DURATION, assert_canonical_total_days

logger = logging.getLogger(__name__)


@dataclass
class AdaptationResult:
    step_ids_to_reschedule: list[int] = field(default_factory=list)
    step_ids_to_cancel: list[int] = field(default_factory=list)


class AdaptationExecutor:
    """Backend executor for plan adaptations.

    Handles plan modification based on adaptation intent.
    Implementation status: Skeleton only (Phase 3: TASK-3.x).
    """

    def _load_plan_with_days(self, db: Session, plan_id: int) -> AIPlan:
        plan = (
            db.query(AIPlan)
            .options(selectinload(AIPlan.days).selectinload(AIPlanDay.steps))
            .filter(AIPlan.id == plan_id)
            .first()
        )
        if not plan:
            raise AdaptationNotEligibleError("plan_not_found")
        return plan

    @staticmethod
    def build_snapshot_before(db: Session, plan: AIPlan) -> dict:
        """Build snapshot_before payload for AdaptationHistory."""
        now_utc = datetime.now(timezone.utc)
        future_steps = []

        for day in plan.days:
            for step in day.steps:
                if step.is_completed or step.skipped or step.canceled_by_adaptation:
                    continue
                if step.scheduled_for and step.scheduled_for <= now_utc:
                    continue
                future_steps.append(
                    {
                        "id": step.id,
                        "day_id": step.day_id,
                        "exercise_id": step.exercise_id,
                        "title": step.title or "",
                        "time_slot": step.time_slot,
                        "slot_type": step.slot_type,
                        "difficulty": str(step.difficulty),
                        "order_in_day": step.order_in_day,
                        "scheduled_for": step.scheduled_for.isoformat() if step.scheduled_for else None,
                        "canceled_by_adaptation": step.canceled_by_adaptation,
                    }
                )

        if len(future_steps) > 500:
            logger.warning(
                "Snapshot too large for plan %s: %d steps. Skipping snapshot.",
                plan.id,
                len(future_steps),
            )
            return {"skipped": True, "reason": "too_large"}

        return {
            "plan_id": plan.id,
            "load": plan.load,
            "focus": plan.focus,
            "total_days": plan.total_days,
            "current_day": plan.current_day or 1,
            "status": plan.status,
            "preferred_time_slots": list(plan.preferred_time_slots or []),
            "future_steps": future_steps,
        }

    def execute(
        self,
        db: Session,
        plan_id: int,
        intent: AdaptationIntent,
        params: dict | None = None,
    ) -> AdaptationResult:
        """Execute adaptation on a plan and return post-commit scheduler actions."""
        plan = self._load_plan_with_days(db, plan_id)
        snapshot = self.build_snapshot_before(db, plan)

        if intent == AdaptationIntent.PAUSE_PLAN:
            reschedule_ids, cancel_ids = self._pause_plan(db, plan)
        elif intent == AdaptationIntent.RESUME_PLAN:
            reschedule_ids, cancel_ids = self._resume_plan(db, plan)
        elif intent == AdaptationIntent.REDUCE_DAILY_LOAD:
            reschedule_ids, cancel_ids = self._reduce_daily_load(db, plan, params)
        elif intent == AdaptationIntent.INCREASE_DAILY_LOAD:
            added_ids = self._increase_daily_load(db, plan, params)
            reschedule_ids, cancel_ids = added_ids, []
        elif intent == AdaptationIntent.LOWER_DIFFICULTY:
            self._lower_difficulty(db, plan.id)
            reschedule_ids, cancel_ids = [], []
        elif intent == AdaptationIntent.INCREASE_DIFFICULTY:
            self._increase_difficulty(db, plan.id)
            reschedule_ids, cancel_ids = [], []
        elif intent == AdaptationIntent.EXTEND_PLAN_DURATION:
            added_ids = self._extend_plan_duration(db, plan, params)
            reschedule_ids, cancel_ids = added_ids, []
        elif intent == AdaptationIntent.SHORTEN_PLAN_DURATION:
            reschedule_ids, cancel_ids = self._shorten_plan_duration(db, plan, params)
        elif intent == AdaptationIntent.CHANGE_MAIN_CATEGORY:
            reschedule_ids, cancel_ids = self._change_main_category(db, plan, params)
        else:
            raise ValueError(f"Unknown adaptation intent: {intent}")

        history_entry = AdaptationHistory(
            plan_id=plan.id,
            user_id=plan.user_id,
            intent=intent.value,
            params=params,
            category=get_adaptation_category(intent),
            snapshot_before=snapshot,
        )
        db.add(history_entry)

        return AdaptationResult(
            step_ids_to_reschedule=reschedule_ids,
            step_ids_to_cancel=cancel_ids,
        )

    def _reduce_daily_load(self, db: Session, plan: AIPlan, params: dict | None = None) -> tuple[list[int], list[int]]:
        from app.plan_adaptations import _iter_future_steps

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
                plan_id=plan.id,
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
                "plan_id": plan.id,
                "adaptation_type": "REDUCE_DAILY_LOAD",
                "slot_removed": slot_to_remove,
                "canceled_step_count": len(canceled_ids),
                "new_load": plan.load,
            },
        )

        return [], canceled_ids

    def _increase_daily_load(self, db: Session, plan: AIPlan, params: dict | None = None) -> list[int]:
        from app.plan_adaptations import _iter_future_steps
        from app.db import ContentLibrary as ContentLibraryModel

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
                plan_id=plan.id,
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
                "plan_id": plan.id,
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

    def _extend_plan_duration(self, db: Session, plan: AIPlan, params: dict | None) -> list[int]:
        from app.plan_drafts.service import build_plan_draft

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
        target_duration_map = {14: "MEDIUM", 21: "STANDARD", 90: "LONG"}
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
                plan_id=plan.id,
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
                plan_id=plan.id,
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
                "plan_id": plan.id,
                "adaptation_type": "EXTEND_PLAN_DURATION",
                "old_total_days": old_total,
                "new_total_days": target_days,
                "added_step_count": len(added_step_ids),
            },
        )

        return added_step_ids

    def _shorten_plan_duration(self, db: Session, plan: AIPlan, params: dict | None) -> tuple[list[int], list[int]]:
        from app.plan_adaptations import _iter_future_steps

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
                plan_id=plan.id,
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
                "plan_id": plan.id,
                "adaptation_type": "SHORTEN_PLAN_DURATION",
                "old_total_days": old_total,
                "new_total_days": target_days,
                "canceled_step_count": len(canceled_ids),
            },
        )

        return [], canceled_ids

    def _pause_plan(self, db: Session, plan: AIPlan) -> tuple[list[int], list[int]]:
        if plan.status == "paused":
            raise AdaptationNotEligibleError("already_paused")
        if plan.status != "active":
            raise AdaptationNotEligibleError("plan_not_active")

        result = apply_plan_adaptation(
            db=db,
            plan_id=plan.id,
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
                "plan_id": plan.id,
                "canceled_step_count": len(result.canceled_step_ids),
            },
        )

        return [], list(result.canceled_step_ids or [])

    def _resume_plan(self, db: Session, plan: AIPlan) -> tuple[list[int], list[int]]:
        if plan.status != "paused":
            raise AdaptationNotEligibleError("not_paused")

        result = apply_plan_adaptation(
            db=db,
            plan_id=plan.id,
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
                "plan_id": plan.id,
                "rescheduled_step_count": len(result.rescheduled_step_ids),
            },
        )

        return list(result.rescheduled_step_ids or []), []

    def rollback_last_adaptation(
        self,
        db: Session,
        plan_id: int,
        history_entry,
    ) -> AdaptationResult:
        """Restore plan to snapshot_before state."""
        if history_entry.is_rolled_back:
            raise AdaptationNotEligibleError("already_rolled_back")

        non_rollback_intents = {"CHANGE_MAIN_CATEGORY", "EXTEND_PLAN_DURATION", "SHORTEN_PLAN_DURATION"}
        if history_entry.intent in non_rollback_intents:
            raise AdaptationNotEligibleError("structural_rollback_not_supported")

        snapshot = history_entry.snapshot_before or {}
        if snapshot.get("skipped"):
            raise AdaptationNotEligibleError("snapshot_not_available")

        plan = self._load_plan_with_days(db, plan_id)

        plan.load = snapshot["load"]
        plan.focus = snapshot["focus"]
        plan.total_days = snapshot["total_days"]
        plan.status = snapshot["status"]
        plan.preferred_time_slots = snapshot["preferred_time_slots"]

        now_utc = datetime.now(timezone.utc)
        canceled_ids: list[int] = []
        for day in plan.days:
            for step in day.steps:
                if not step.is_completed and not step.skipped:
                    step.canceled_by_adaptation = True
                    step.scheduled_for = None
                    canceled_ids.append(step.id)

        db.flush()

        days_by_number = {d.day_number: d for d in plan.days}
        restored_ids: list[int] = []

        all_day_ids = list({step["day_id"] for step in snapshot.get("future_steps", [])})
        days_by_id: dict[int, int] = {}
        if all_day_ids:
            day_rows = (
                db.query(AIPlanDay.id, AIPlanDay.day_number)
                .filter(AIPlanDay.id.in_(all_day_ids))
                .all()
            )
            days_by_id = {row.id: row.day_number for row in day_rows}

        for step_data in snapshot.get("future_steps", []):
            day_number = days_by_id.get(step_data["day_id"])
            if day_number is None:
                continue
            day_record = days_by_number.get(day_number)
            if not day_record:
                continue

            new_scheduled_for = _recalculate_scheduled_for(
                plan_start=plan.start_date,
                day_number=day_number,
                time_slot=step_data["time_slot"],
                current_day=plan.current_day or 1,
                now_utc=now_utc,
            )
            if new_scheduled_for is None:
                continue

            new_step = AIPlanStep(
                day_id=day_record.id,
                exercise_id=step_data["exercise_id"],
                title=step_data["title"],
                time_slot=step_data["time_slot"],
                slot_type=step_data["slot_type"],
                difficulty=step_data["difficulty"],
                order_in_day=step_data["order_in_day"],
                scheduled_for=new_scheduled_for,
                canceled_by_adaptation=False,
            )
            db.add(new_step)
            db.flush()
            restored_ids.append(new_step.id)

        history_entry.is_rolled_back = True
        history_entry.rolled_back_at = now_utc

        log_user_event(
            db=db,
            user_id=plan.user_id,
            event_type="adaptation_rolled_back",
            context={
                "plan_id": plan_id,
                "rolled_back_intent": history_entry.intent,
                "restored_step_count": len(restored_ids),
            },
        )

        return AdaptationResult(
            step_ids_to_reschedule=restored_ids,
            step_ids_to_cancel=canceled_ids,
        )

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

    def _change_main_category(self, db: Session, plan: AIPlan, params: dict | None) -> tuple[list[int], list[int]]:
        """Change main focus category by pausing old plan and creating new active plan."""
        from app.plan_adaptations import _iter_future_steps
        from app.plan_drafts.draft_builder import DraftValidationError
        from app.plan_drafts.service import InsufficientLibraryError, build_plan_draft

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
            .filter(AIPlan.user_id == plan.user_id, AIPlan.status == "active", AIPlan.id != plan.id)
            .first()
        )
        if other_active:
            raise AdaptationNotEligibleError("another_active_plan_exists")

        _DAYS_TO_DURATION = DAYS_TO_DURATION
        draft_duration = _DAYS_TO_DURATION.get(plan.total_days)
        if draft_duration is None:
            logger.error(
                "[CHANGE_MAIN_CATEGORY] Invalid total_days=%s for plan %s",
                plan.total_days,
                plan.id,
            )
            raise AdaptationNotEligibleError("invalid_plan_duration")

        params_dict = {
            "duration": draft_duration,
            "focus": target_category,
            "load": plan.load,
            "preferred_time_slots": list(plan.preferred_time_slots),
        }
        try:
            draft = build_plan_draft(params_dict, user_id=str(plan.user_id))
        except (InsufficientLibraryError, DraftValidationError, TypeError) as exc:
            raise AdaptationNotEligibleError("content_library_insufficient") from exc

        if draft.total_days < plan.total_days:
            raise AdaptationNotEligibleError("content_library_insufficient")

        effective_from = datetime.now(timezone.utc)
        canceled_ids: list[int] = []
        for _day, step in _iter_future_steps(plan, effective_from):
            step.canceled_by_adaptation = True
            step.scheduled_for = None
            canceled_ids.append(step.id)

        old_focus = plan.focus
        plan.status = "paused"

        new_total_days = plan.total_days
        try:
            assert_canonical_total_days(new_total_days)
        except ValueError as exc:
            raise AdaptationNotEligibleError("invalid_plan_duration") from exc
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

        filtered_steps = [step for step in draft.steps if step.day_number <= new_total_days]

        days_dict: dict[int, list] = {}
        for step in filtered_steps:
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
                plan_id=plan.id,
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
                "plan_id": plan.id,
                "adaptation_type": "CHANGE_MAIN_CATEGORY",
                "old_focus": old_focus,
                "new_focus": target_category,
                "new_plan_id": new_plan.id,
                "canceled_step_count": len(canceled_ids),
                "added_step_count": len(added_step_ids),
            },
        )

        return added_step_ids, canceled_ids



def _recalculate_scheduled_for(
    plan_start: datetime,
    day_number: int,
    time_slot: str,
    current_day: int,
    now_utc: datetime,
) -> datetime | None:
    """Compute new scheduled_for for a restored step."""
    from datetime import time

    _SLOT_TIMES = {
        "MORNING": time(9, 30),
        "DAY": time(14, 0),
        "EVENING": time(21, 0),
    }
    slot_time = _SLOT_TIMES.get(time_slot.upper())
    if not slot_time:
        return None

    target_date = (plan_start + timedelta(days=day_number - 1)).date()
    naive = datetime.combine(target_date, slot_time)
    scheduled = naive.replace(tzinfo=timezone.utc)

    if scheduled <= now_utc:
        return None

    return scheduled

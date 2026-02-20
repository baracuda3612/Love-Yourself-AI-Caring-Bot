from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session, selectinload

from app.adaptation_types import AdaptationIntent, AdaptationNotEligibleError
from app.db import AIPlan, AIPlanDay, AIPlanStep
from app.plan_adaptations import apply_plan_adaptation
from app.scheduler import cancel_plan_step_jobs, reschedule_plan_steps
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
            self._reduce_daily_load(db, plan_id)
        elif intent == AdaptationIntent.INCREASE_DAILY_LOAD:
            self._increase_daily_load(db, plan_id)
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

    def _reduce_daily_load(self, db: Session, plan_id: int) -> None:
        """Reduce daily task count by 1 (TASK-3.1)."""
        raise NotImplementedError("REDUCE_DAILY_LOAD implementation in TASK-3.1")

    def _increase_daily_load(self, db: Session, plan_id: int) -> None:
        """Increase daily task count by 1 (TASK-3.2)."""
        raise NotImplementedError("INCREASE_DAILY_LOAD implementation in TASK-3.2")

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

    def _change_main_category(self, db: Session, plan_id: int, params: dict | None) -> None:
        """Change main focus category (TASK-3.6)."""
        raise NotImplementedError("CHANGE_MAIN_CATEGORY implementation in TASK-3.6")

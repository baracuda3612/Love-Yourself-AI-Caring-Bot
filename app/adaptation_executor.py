"""Adaptation executor (backend implementation).

This module handles the actual execution of adaptations.
Implementation will be added in Phase 3 (TASK-3.x).

NOTE: Database imports are reserved for Phase 3 implementation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

from app.adaptation_types import AdaptationIntent


class AdaptationExecutor:
    """Backend executor for plan adaptations.

    Handles plan modification based on adaptation intent.
    Implementation status: Skeleton only (Phase 3: TASK-3.x).
    """

    def execute(
        self,
        db: "Session",
        plan_id: int,
        intent: AdaptationIntent,
        params: dict | None = None,
    ) -> None:
        """Execute adaptation on a plan."""
        if intent == AdaptationIntent.REDUCE_DAILY_LOAD:
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
        elif intent == AdaptationIntent.PAUSE_PLAN:
            self._pause_plan(db, plan_id)
        elif intent == AdaptationIntent.RESUME_PLAN:
            self._resume_plan(db, plan_id)
        elif intent == AdaptationIntent.CHANGE_MAIN_CATEGORY:
            self._change_main_category(db, plan_id, params)
        else:
            raise ValueError(f"Unknown adaptation intent: {intent}")

    def _reduce_daily_load(self, db: "Session", plan_id: int) -> None:
        """Reduce daily task count by 1 (TASK-3.1)."""
        raise NotImplementedError("REDUCE_DAILY_LOAD implementation in TASK-3.1")

    def _increase_daily_load(self, db: "Session", plan_id: int) -> None:
        """Increase daily task count by 1 (TASK-3.2)."""
        raise NotImplementedError("INCREASE_DAILY_LOAD implementation in TASK-3.2")

    def _lower_difficulty(self, db: "Session", plan_id: int) -> None:
        """Lower difficulty by 1 level (TASK-3.5)."""
        raise NotImplementedError("LOWER_DIFFICULTY implementation in TASK-3.5")

    def _increase_difficulty(self, db: "Session", plan_id: int) -> None:
        """Increase difficulty by 1 level (TASK-3.5)."""
        raise NotImplementedError("INCREASE_DIFFICULTY implementation in TASK-3.5")

    def _extend_plan_duration(self, db: "Session", plan_id: int, params: dict | None) -> None:
        """Extend plan duration (TASK-3.4)."""
        raise NotImplementedError("EXTEND_PLAN_DURATION implementation in TASK-3.4")

    def _shorten_plan_duration(self, db: "Session", plan_id: int, params: dict | None) -> None:
        """Shorten plan duration (TASK-3.4)."""
        raise NotImplementedError("SHORTEN_PLAN_DURATION implementation in TASK-3.4")

    def _pause_plan(self, db: "Session", plan_id: int) -> None:
        """Pause plan execution (TASK-3.3)."""
        raise NotImplementedError("PAUSE_PLAN implementation in TASK-3.3")

    def _resume_plan(self, db: "Session", plan_id: int) -> None:
        """Resume paused plan (TASK-3.3)."""
        raise NotImplementedError("RESUME_PLAN implementation in TASK-3.3")

    def _change_main_category(self, db: "Session", plan_id: int, params: dict | None) -> None:
        """Change main focus category (TASK-3.6)."""
        raise NotImplementedError("CHANGE_MAIN_CATEGORY implementation in TASK-3.6")

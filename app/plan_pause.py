"""Plan pause/resume compatibility API backed only by ``AIPlan.status``."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.lifecycle import LifecycleTransitionError, transition_current_plan


# ─── Errors ──────────────────────────────────────────────────────────────────


class PlanNotActiveError(RuntimeError):
    """User is not in ACTIVE state — cannot pause."""


class PlanAlreadyPausedError(RuntimeError):
    """Plan is already paused."""


class PlanNotPausedError(RuntimeError):
    """Plan is not paused — cannot resume."""


# ─── Public API ───────────────────────────────────────────────────────────────


def pause_plan(db: Session, user_id: int, *, source_operation_id: str):
    """
    Pause plan delivery for a user.

    Caller owns commit; the operation locks the aggregate and records an
    idempotency receipt before returning.
    """
    try:
        return transition_current_plan(
            db,
            user_id=user_id,
            operation="pause",
            source_operation_id=source_operation_id,
        )
    except LifecycleTransitionError as exc:
        if "got paused" in str(exc):
            raise PlanAlreadyPausedError(str(exc)) from exc
        raise PlanNotActiveError(str(exc)) from exc


def resume_plan(db: Session, user_id: int, *, source_operation_id: str):
    """
    Resume plan delivery for a user.

    Caller owns commit; no user/profile lifecycle mirror is touched.
    """
    try:
        return transition_current_plan(
            db,
            user_id=user_id,
            operation="resume",
            source_operation_id=source_operation_id,
        )
    except LifecycleTransitionError as exc:
        raise PlanNotPausedError(str(exc)) from exc

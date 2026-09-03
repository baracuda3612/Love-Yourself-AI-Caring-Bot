"""Plan-centric lifecycle authority and operation-level mutation boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db import (
    AIPlan,
    AIPlanDay,
    AIPlanStep,
    OnboardingProgress,
    PlanLifecycleOperation,
    User,
)


CURRENT_PLAN_STATUSES = ("active", "paused")
TERMINAL_STEP_STATUSES = ("completed", "skipped", "expired", "canceled")


class CurrentMode(str, Enum):
    ONBOARDING = "ONBOARDING"
    ACTIVE = "ACTIVE"
    ACTIVE_PAUSED = "ACTIVE_PAUSED"
    NO_ACTIVE_PLAN = "NO_ACTIVE_PLAN"


class LifecycleInvariantError(RuntimeError):
    """Stored rows do not form one valid lifecycle aggregate."""


class LifecycleTransitionError(RuntimeError):
    """The requested operation is stale or invalid for authoritative state."""


@dataclass(frozen=True)
class LifecycleResult:
    plan_id: int
    status: str
    operation: str
    duplicate: bool = False
    step_id: int | None = None


def derive_current_mode_from_facts(
    *,
    onboarding_stage: str | None,
    onboarding_completed_at: datetime | None,
    current_plan_status: str | None,
) -> CurrentMode:
    """Pure mode derivation used by every UI, Coach, and scheduler reader."""
    if current_plan_status == "active":
        return CurrentMode.ACTIVE
    if current_plan_status == "paused":
        return CurrentMode.ACTIVE_PAUSED
    if current_plan_status is not None:
        raise LifecycleInvariantError(f"non-current status passed as current: {current_plan_status}")

    normalized_stage = str(onboarding_stage or "").strip().upper()
    if onboarding_completed_at is not None or normalized_stage == "COMPLETED":
        return CurrentMode.NO_ACTIVE_PLAN
    return CurrentMode.ONBOARDING


def _current_plans_query(db: Session, user_id: int):
    return db.query(AIPlan).filter(
        AIPlan.user_id == user_id,
        AIPlan.status.in_(CURRENT_PLAN_STATUSES),
    )


def get_current_plan(db: Session, user_id: int, *, lock: bool = False) -> AIPlan | None:
    query = _current_plans_query(db, user_id).order_by(AIPlan.id.asc()).limit(2)
    if lock:
        query = query.with_for_update()
    plans = query.all()
    if len(plans) > 1:
        raise LifecycleInvariantError(f"user {user_id} has multiple current plans")
    return plans[0] if plans else None


def derive_current_mode(db: Session, user_id: int) -> CurrentMode:
    plan = get_current_plan(db, user_id)
    progress = (
        db.query(OnboardingProgress)
        .filter(OnboardingProgress.user_id == user_id)
        .first()
    )
    return derive_current_mode_from_facts(
        onboarding_stage=progress.stage if progress else None,
        onboarding_completed_at=progress.completed_at if progress else None,
        current_plan_status=str(plan.status) if plan else None,
    )


def ensure_onboarding_progress(
    db: Session,
    user_id: int,
    *,
    stage: str = "START",
) -> OnboardingProgress:
    progress = (
        db.query(OnboardingProgress)
        .filter(OnboardingProgress.user_id == user_id)
        .first()
    )
    if progress is None:
        progress = OnboardingProgress(user_id=user_id, stage=stage)
        db.add(progress)
        db.flush()
    return progress


def mark_onboarding_completed(db: Session, user_id: int) -> OnboardingProgress:
    progress = ensure_onboarding_progress(db, user_id)
    progress.stage = "COMPLETED"
    if progress.completed_at is None:
        progress.completed_at = datetime.now(timezone.utc)
    return progress


def _lock_user(db: Session, user_id: int) -> User:
    user = db.query(User).filter(User.id == user_id).with_for_update().first()
    if user is None:
        raise LifecycleInvariantError(f"user {user_id} not found")
    return user


def find_lifecycle_operation(
    db: Session,
    user_id: int,
    source_operation_id: str,
) -> PlanLifecycleOperation | None:
    return (
        db.query(PlanLifecycleOperation)
        .filter(
            PlanLifecycleOperation.user_id == user_id,
            PlanLifecycleOperation.source_operation_id == source_operation_id,
        )
        .first()
    )


def _operation_result(receipt: PlanLifecycleOperation) -> LifecycleResult:
    return LifecycleResult(
        plan_id=receipt.plan_id,
        step_id=receipt.plan_step_id,
        status=receipt.result_status,
        operation=receipt.operation,
        duplicate=True,
    )


def record_lifecycle_operation(
    db: Session,
    *,
    user_id: int,
    plan_id: int,
    source_operation_id: str,
    operation: str,
    result_status: str,
    step_id: int | None = None,
) -> None:
    if not source_operation_id or len(source_operation_id) > 160:
        raise LifecycleTransitionError("invalid source_operation_id")
    db.add(
        PlanLifecycleOperation(
            user_id=user_id,
            plan_id=plan_id,
            plan_step_id=step_id,
            source_operation_id=source_operation_id,
            operation=operation,
            result_status=result_status,
        )
    )


def _duplicate_operation_result(
    receipt: PlanLifecycleOperation,
    *,
    expected_operation: str,
    expected_plan_id: int | None = None,
    expected_step_id: int | None = None,
) -> LifecycleResult:
    if receipt.operation != expected_operation:
        raise LifecycleTransitionError(
            "source_operation_id already belongs to "
            f"{receipt.operation}, not {expected_operation}"
        )
    if expected_plan_id is not None and receipt.plan_id != expected_plan_id:
        raise LifecycleTransitionError(
            "source_operation_id already belongs to plan "
            f"{receipt.plan_id}, not {expected_plan_id}"
        )
    if expected_step_id is not None and receipt.plan_step_id != expected_step_id:
        raise LifecycleTransitionError(
            "source_operation_id already belongs to plan step "
            f"{receipt.plan_step_id}, not {expected_step_id}"
        )
    return _operation_result(receipt)


_PLAN_TRANSITIONS = {
    "pause": ({"active"}, "paused"),
    "resume": ({"paused"}, "active"),
}


def transition_current_plan(
    db: Session,
    *,
    user_id: int,
    operation: str,
    source_operation_id: str,
) -> LifecycleResult:
    """Lock the user/current plan and apply pause or resume exactly once."""
    if operation not in _PLAN_TRANSITIONS:
        raise LifecycleTransitionError(f"unsupported plan operation: {operation}")
    _lock_user(db, user_id)
    existing = find_lifecycle_operation(db, user_id, source_operation_id)
    if existing:
        return _duplicate_operation_result(existing, expected_operation=operation)

    plan = get_current_plan(db, user_id, lock=True)
    if plan is None:
        raise LifecycleTransitionError("current_plan_missing")
    allowed, target = _PLAN_TRANSITIONS[operation]
    current = str(plan.status)
    if current not in allowed:
        raise LifecycleTransitionError(f"{operation} requires {sorted(allowed)}, got {current}")

    plan.status = target
    plan.version = int(plan.version or 0) + 1
    record_lifecycle_operation(
        db,
        user_id=user_id,
        plan_id=plan.id,
        source_operation_id=source_operation_id,
        operation=operation,
        result_status=target,
    )
    db.flush()
    return LifecycleResult(plan_id=plan.id, status=target, operation=operation)


def abandon_current_plan(
    db: Session,
    *,
    user_id: int,
    source_operation_id: str,
    occurred_at: datetime | None = None,
) -> tuple[LifecycleResult, list[int]]:
    """Atomically abandon one current plan and cancel all open child steps."""
    _lock_user(db, user_id)
    existing = find_lifecycle_operation(db, user_id, source_operation_id)
    if existing:
        return (
            _duplicate_operation_result(existing, expected_operation="abandon"),
            [],
        )

    plan = get_current_plan(db, user_id, lock=True)
    if plan is None:
        raise LifecycleTransitionError("current_plan_missing")
    now = occurred_at or datetime.now(timezone.utc)
    open_steps = (
        db.query(AIPlanStep)
        .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
        .filter(
            AIPlanDay.plan_id == plan.id,
            AIPlanStep.step_status.in_(("pending", "delivered")),
        )
        .with_for_update()
        .all()
    )
    for step in open_steps:
        step.step_status = "canceled"
        step.terminal_at = now
        step.version = max(0, int(step.version or 0)) + 1

    plan.status = "abandoned"
    plan.abandoned_at = now
    plan.version = int(plan.version or 0) + 1
    record_lifecycle_operation(
        db,
        user_id=user_id,
        plan_id=plan.id,
        source_operation_id=source_operation_id,
        operation="abandon",
        result_status="abandoned",
    )
    db.flush()
    return (
        LifecycleResult(plan_id=plan.id, status="abandoned", operation="abandon"),
        [step.id for step in open_steps],
    )


def transition_plan_step(
    db: Session,
    *,
    user_id: int,
    step_id: int,
    target_status: str,
    source_operation_id: str,
    occurred_at: datetime | None = None,
) -> LifecycleResult:
    """Conditionally transition one owned step; terminal state has one winner."""
    if target_status not in {"delivered", *TERMINAL_STEP_STATUSES}:
        raise LifecycleTransitionError(f"unsupported step target: {target_status}")
    _lock_user(db, user_id)
    expected_operation = f"step_{target_status}"
    existing = find_lifecycle_operation(db, user_id, source_operation_id)
    if existing:
        return _duplicate_operation_result(
            existing,
            expected_operation=expected_operation,
            expected_step_id=step_id,
        )

    step = (
        db.query(AIPlanStep)
        .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
        .join(AIPlan, AIPlan.id == AIPlanDay.plan_id)
        .filter(AIPlanStep.id == step_id, AIPlan.user_id == user_id)
        .with_for_update()
        .first()
    )
    if step is None:
        raise LifecycleTransitionError("plan_step_missing")
    plan = step.day.plan
    if str(plan.status) != "active":
        raise LifecycleTransitionError("plan_not_active")

    current = str(step.step_status)
    if current in TERMINAL_STEP_STATUSES:
        if current != target_status:
            raise LifecycleTransitionError(f"terminal step already won with {current}")
        result = LifecycleResult(
            plan_id=plan.id,
            step_id=step.id,
            status=current,
            operation=f"step_{target_status}",
            duplicate=True,
        )
        return result
    if target_status == "delivered" and current not in {"pending", "delivered"}:
        raise LifecycleTransitionError(f"cannot deliver from {current}")
    if target_status in TERMINAL_STEP_STATUSES and current not in {"pending", "delivered"}:
        raise LifecycleTransitionError(f"cannot finish from {current}")

    step.step_status = target_status
    step.terminal_at = (
        occurred_at or datetime.now(timezone.utc)
        if target_status in TERMINAL_STEP_STATUSES
        else None
    )
    step.version = max(0, int(step.version or 0)) + 1
    record_lifecycle_operation(
        db,
        user_id=user_id,
        plan_id=plan.id,
        step_id=step.id,
        source_operation_id=source_operation_id,
        operation=expected_operation,
        result_status=target_status,
    )
    db.flush()
    return LifecycleResult(
        plan_id=plan.id,
        step_id=step.id,
        status=target_status,
        operation=expected_operation,
        duplicate=current == target_status,
    )


def complete_current_plan_if_ready(
    db: Session,
    *,
    user_id: int,
    plan_id: int,
    source_operation_id: str,
) -> LifecycleResult | None:
    """Complete an active plan only after every child step is terminal."""
    _lock_user(db, user_id)
    existing = find_lifecycle_operation(db, user_id, source_operation_id)
    if existing:
        return _duplicate_operation_result(
            existing,
            expected_operation="complete",
            expected_plan_id=plan_id,
        )

    plan = get_current_plan(db, user_id, lock=True)
    if plan is None or plan.id != plan_id:
        return None
    if str(plan.status) != "active":
        return None

    total_steps = (
        db.query(func.count(AIPlanStep.id))
        .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
        .filter(AIPlanDay.plan_id == plan.id)
        .scalar()
    )
    open_steps = (
        db.query(func.count(AIPlanStep.id))
        .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
        .filter(
            AIPlanDay.plan_id == plan.id,
            AIPlanStep.step_status.notin_(TERMINAL_STEP_STATUSES),
        )
        .scalar()
    )
    unproven_terminal = (
        db.query(func.count(AIPlanStep.id))
        .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
        .filter(
            AIPlanDay.plan_id == plan.id,
            AIPlanStep.step_status.in_(TERMINAL_STEP_STATUSES),
            AIPlanStep.terminal_at.is_(None),
            AIPlanStep.version != 0,
        )
        .scalar()
    )
    if not total_steps or open_steps or unproven_terminal:
        return None

    plan.status = "completed"
    plan.version = int(plan.version or 0) + 1
    record_lifecycle_operation(
        db,
        user_id=user_id,
        plan_id=plan.id,
        source_operation_id=source_operation_id,
        operation="complete",
        result_status="completed",
    )
    db.flush()
    return LifecycleResult(plan_id=plan.id, status="completed", operation="complete")


def plan_completion_at(db: Session, plan_id: int) -> datetime | None:
    """Calculate completion chronology from authoritative terminal step facts."""
    return (
        db.query(func.max(AIPlanStep.terminal_at))
        .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
        .filter(AIPlanDay.plan_id == plan_id)
        .scalar()
    )


def derive_current_day(db: Session, plan_id: int, total_days: int) -> int:
    """Calculate the first day with open work, or the final day if all terminal."""
    open_day = (
        db.query(func.min(AIPlanDay.day_number))
        .join(AIPlanStep, AIPlanStep.day_id == AIPlanDay.id)
        .filter(
            AIPlanDay.plan_id == plan_id,
            AIPlanStep.step_status.notin_(TERMINAL_STEP_STATUSES),
        )
        .scalar()
    )
    if open_day is not None:
        return max(1, min(int(open_day), int(total_days)))
    return max(1, int(total_days))

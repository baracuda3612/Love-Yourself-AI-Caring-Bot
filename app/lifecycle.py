"""Authoritative plan lifecycle reads, decisions, and mutation boundaries.

Every deterministic surface enters plan lifecycle through this module.  The
functions here own persisted decisions and return explicit external-effect
intents; callers commit first and reconcile scheduler or Telegram effects
separately.  Persistence code never sends an ambient message.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any

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


class LifecycleOwnershipError(LifecycleTransitionError):
    """The actor does not own the requested lifecycle aggregate."""


class LifecycleEntitlementError(LifecycleTransitionError):
    """The current runtime entitlement does not permit lifecycle work."""


class ExternalEffectState(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEFERRED = "deferred"
    NOT_REQUIRED = "not_required"


@dataclass(frozen=True)
class ExternalEffect:
    """Observable work that must run only after the lifecycle commit."""

    kind: str
    target_ids: tuple[int, ...] = ()
    state: ExternalEffectState = ExternalEffectState.PENDING
    attempted: int = 0
    succeeded: int = 0
    error_code: str | None = None


@dataclass(frozen=True)
class LifecycleResult:
    plan_id: int
    status: str
    operation: str
    duplicate: bool = False
    step_id: int | None = None
    user_id: int | None = None
    code: str = "applied"
    applied: bool = True
    day_number: int | None = None
    plan_type: str | None = None
    effects: tuple[ExternalEffect, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def external_effects_succeeded(self) -> bool:
        return all(
            effect.state
            in {
                ExternalEffectState.SUCCEEDED,
                ExternalEffectState.DEFERRED,
                ExternalEffectState.NOT_REQUIRED,
            }
            for effect in self.effects
        )


@dataclass(frozen=True)
class LifecycleStatus:
    """Authoritative current-plan and remaining-delivery facts."""

    user_id: int
    current_mode: CurrentMode
    plan_id: int | None = None
    plan_status: str | None = None
    plan_type: str | None = None
    days_total: int = 0
    current_day: int | None = None
    days_completed: int = 0
    delivery_days_remaining: int = 0
    steps_total: int = 0
    steps_completed: int = 0
    steps_remaining: int = 0
    deliveries_remaining: int = 0

    @property
    def completion_rate(self) -> int:
        if not self.steps_total:
            return 0
        return round((self.steps_completed / self.steps_total) * 100)

    def to_runtime_payload(self) -> dict[str, Any]:
        if self.plan_id is None:
            return {
                "state": self.current_mode.value,
                "current_mode": self.current_mode.value,
                "plan_active": False,
            }
        return {
            "state": self.current_mode.value,
            "current_mode": self.current_mode.value,
            "plan_active": True,
            "plan_id": self.plan_id,
            "plan_type": self.plan_type,
            "days_total": self.days_total,
            "current_day": self.current_day,
            "days_completed": self.days_completed,
            "days_remaining": self.delivery_days_remaining,
            "steps_total": self.steps_total,
            "steps_completed": self.steps_completed,
            "steps_remaining": self.steps_remaining,
            "deliveries_remaining": self.deliveries_remaining,
            "completion_rate": self.completion_rate,
        }


@dataclass(frozen=True)
class ContinuationInterfaceResult:
    """Stable WP-03.5 hand-off without creating the next plan early."""

    user_id: int
    completed_plan_id: int
    plan_type: str
    source_operation_id: str
    status: str = "deferred_to_wp_03_5"


@dataclass(frozen=True)
class CompletionDeliveryResult:
    """Observable outcome of the explicit completion-report effect."""

    user_id: int
    plan_id: int
    succeeded: bool
    duplicate: bool = False
    retry_scheduled: bool = False
    code: str = "sent"


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
    query = (
        _current_plans_query(db, user_id)
        .populate_existing()
        .order_by(AIPlan.id.asc())
        .limit(2)
    )
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


def read_lifecycle_status(db: Session, user_id: int) -> LifecycleStatus:
    """Re-read the persisted aggregate and derive truthful delivery facts."""
    _lock_user(db, user_id)
    plan = get_current_plan(db, user_id, lock=True)
    progress = (
        db.query(OnboardingProgress)
        .filter(OnboardingProgress.user_id == user_id)
        .populate_existing()
        .first()
    )
    mode = derive_current_mode_from_facts(
        onboarding_stage=progress.stage if progress else None,
        onboarding_completed_at=progress.completed_at if progress else None,
        current_plan_status=str(plan.status) if plan else None,
    )
    if plan is None:
        return LifecycleStatus(user_id=user_id, current_mode=mode)

    base_steps = (
        db.query(AIPlanStep)
        .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
        .filter(AIPlanDay.plan_id == plan.id)
    )
    steps_total = base_steps.filter(AIPlanStep.step_status != "canceled").count()
    steps_completed = base_steps.filter(AIPlanStep.step_status == "completed").count()
    steps_remaining = base_steps.filter(
        AIPlanStep.step_status.in_(("pending", "delivered"))
    ).count()
    deliveries_remaining = base_steps.filter(
        AIPlanStep.step_status == "pending"
    ).count()
    delivery_days_remaining = (
        db.query(func.count(func.distinct(AIPlanDay.id)))
        .join(AIPlanStep, AIPlanStep.day_id == AIPlanDay.id)
        .filter(
            AIPlanDay.plan_id == plan.id,
            AIPlanStep.step_status == "pending",
        )
        .scalar()
        or 0
    )
    open_days = (
        db.query(func.count(func.distinct(AIPlanDay.id)))
        .join(AIPlanStep, AIPlanStep.day_id == AIPlanDay.id)
        .filter(
            AIPlanDay.plan_id == plan.id,
            AIPlanStep.step_status.in_(("pending", "delivered")),
        )
        .scalar()
        or 0
    )
    current_day = (
        db.query(func.min(AIPlanDay.day_number))
        .join(AIPlanStep, AIPlanStep.day_id == AIPlanDay.id)
        .filter(
            AIPlanDay.plan_id == plan.id,
            AIPlanStep.step_status.in_(("pending", "delivered")),
        )
        .scalar()
    )
    days_total = int(plan.total_days or 0)
    if current_day is None and days_total:
        current_day = days_total

    return LifecycleStatus(
        user_id=user_id,
        current_mode=mode,
        plan_id=plan.id,
        plan_status=str(plan.status),
        plan_type=_plan_type(plan),
        days_total=days_total,
        current_day=int(current_day) if current_day is not None else None,
        days_completed=max(0, days_total - int(open_days)),
        delivery_days_remaining=int(delivery_days_remaining),
        steps_total=int(steps_total),
        steps_completed=int(steps_completed),
        steps_remaining=int(steps_remaining),
        deliveries_remaining=int(deliveries_remaining),
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


def _lock_user(
    db: Session,
    user_id: int,
    *,
    require_entitlement: bool = True,
) -> User:
    user = (
        db.query(User)
        .filter(User.id == user_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if user is None:
        raise LifecycleInvariantError(f"user {user_id} not found")
    if require_entitlement and getattr(user, "is_active", True) is not True:
        raise LifecycleEntitlementError("user_not_entitled")
    return user


def require_lifecycle_entitlement(db: Session, user_id: int) -> None:
    """Re-read and enforce the current runtime entitlement under the user lock."""
    _lock_user(db, user_id)


def _lock_telegram_actor(db: Session, telegram_user_id: int) -> User:
    user = (
        db.query(User)
        .filter(User.tg_id == telegram_user_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if user is None:
        raise LifecycleOwnershipError("telegram_actor_not_found")
    if getattr(user, "is_active", True) is not True:
        raise LifecycleEntitlementError("user_not_entitled")
    return user


def _plan_type(plan: AIPlan) -> str:
    total_days = int(plan.total_days or 0)
    if total_days == 7:
        return "SHORT"
    if total_days == 14:
        return "MEDIUM"
    raise LifecycleInvariantError(f"plan {plan.id} has unsupported duration")


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
        user_id=receipt.user_id,
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
    expected_result_status: str | None = None,
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
    if (
        expected_result_status is not None
        and receipt.result_status != expected_result_status
    ):
        raise LifecycleTransitionError(
            "source_operation_id already recorded result "
            f"{receipt.result_status}, not {expected_result_status}"
        )
    return _operation_result(receipt)


def activate_plan(
    db: Session,
    *,
    user_id: int,
    plan_type: str,
    day_time: str,
    evening_time: str | None,
    source_operation_id: str,
    require_plan_history: bool = False,
) -> LifecycleResult:
    """Create one plan under the lifecycle lock and expose schedule work."""
    _lock_user(db, user_id)
    existing = find_lifecycle_operation(db, user_id, source_operation_id)
    if existing is None and require_plan_history:
        if derive_current_mode(db, user_id) is not CurrentMode.NO_ACTIVE_PLAN:
            raise LifecycleTransitionError(
                "followup_activation_requires_no_active_plan"
            )
        if db.query(AIPlan.id).filter(AIPlan.user_id == user_id).first() is None:
            raise LifecycleTransitionError("followup_activation_requires_plan_history")
    from app.plan_drafts.service import create_plan_for_lifecycle

    activation = create_plan_for_lifecycle(
        db,
        user_id=user_id,
        plan_type=plan_type,
        day_time=day_time,
        evening_time=evening_time,
        source_operation_id=source_operation_id,
    )
    plan = activation.plan
    step_ids = tuple(
        step_id
        for (step_id,) in (
            db.query(AIPlanStep.id)
            .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
            .filter(
                AIPlanDay.plan_id == plan.id,
                AIPlanStep.step_status == "pending",
            )
            .order_by(AIPlanStep.id)
            .all()
        )
    )
    effect_state = (
        ExternalEffectState.PENDING
        if str(plan.status) == "active" and step_ids
        else ExternalEffectState.NOT_REQUIRED
    )
    return LifecycleResult(
        user_id=user_id,
        plan_id=plan.id,
        status=str(plan.status),
        operation="activate",
        duplicate=activation.duplicate,
        plan_type=_plan_type(plan),
        effects=(
            ExternalEffect(
                kind="reconcile_plan_schedule",
                target_ids=step_ids,
                state=effect_state,
            ),
        ),
        details={"total_days": int(plan.total_days)},
    )


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
        duplicate = _duplicate_operation_result(
            existing,
            expected_operation=operation,
        )
        return replace(
            duplicate,
            code=f"{operation}_schedule_semantics_deferred_to_wp_02_3",
            effects=(
                ExternalEffect(
                    kind=f"{operation}_schedule_reconciliation",
                    state=ExternalEffectState.DEFERRED,
                ),
            ),
        )

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
    return LifecycleResult(
        user_id=user_id,
        plan_id=plan.id,
        status=target,
        operation=operation,
        code=f"{operation}_schedule_semantics_deferred_to_wp_02_3",
        plan_type=_plan_type(plan),
        effects=(
            ExternalEffect(
                kind=f"{operation}_schedule_reconciliation",
                state=ExternalEffectState.DEFERRED,
            ),
        ),
    )


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
        duplicate = _duplicate_operation_result(
            existing,
            expected_operation="abandon",
        )
        abandoned_plan = (
            db.query(AIPlan)
            .filter(AIPlan.id == existing.plan_id, AIPlan.user_id == user_id)
            .populate_existing()
            .first()
        )
        if abandoned_plan is None:
            raise LifecycleInvariantError("abandon_receipt_plan_missing")
        canceled_ids = tuple(
            step_id
            for (step_id,) in (
                db.query(AIPlanStep.id)
                .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
                .filter(
                    AIPlanDay.plan_id == existing.plan_id,
                    AIPlanStep.step_status == "canceled",
                )
                .order_by(AIPlanStep.id)
                .all()
            )
        )
        return (
            replace(
                duplicate,
                plan_type=_plan_type(abandoned_plan),
                effects=(
                    ExternalEffect(
                        kind="cancel_step_jobs",
                        target_ids=canceled_ids,
                        state=(
                            ExternalEffectState.PENDING
                            if canceled_ids
                            else ExternalEffectState.NOT_REQUIRED
                        ),
                    ),
                ),
            ),
            list(canceled_ids),
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
        .populate_existing()
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
        LifecycleResult(
            user_id=user_id,
            plan_id=plan.id,
            status="abandoned",
            operation="abandon",
            plan_type=_plan_type(plan),
            effects=(
                ExternalEffect(
                    kind="cancel_step_jobs",
                    target_ids=tuple(step.id for step in open_steps),
                    state=(
                        ExternalEffectState.PENDING
                        if open_steps
                        else ExternalEffectState.NOT_REQUIRED
                    ),
                ),
            ),
        ),
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
        duplicate = _duplicate_operation_result(
            existing,
            expected_operation=expected_operation,
            expected_step_id=step_id,
        )
        if target_status != "expired":
            return duplicate
        step = db.query(AIPlanStep).filter(AIPlanStep.id == step_id).first()
        target_ids = (
            (step_id,)
            if step is not None and getattr(step, "tg_message_id", None)
            else ()
        )
        return replace(
            duplicate,
            effects=(
                ExternalEffect(
                    kind="remove_step_keyboard",
                    target_ids=target_ids,
                    state=(
                        ExternalEffectState.PENDING
                        if target_ids
                        else ExternalEffectState.NOT_REQUIRED
                    ),
                ),
            ),
        )

    row = (
        db.query(AIPlanStep, AIPlan)
        .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
        .join(AIPlan, AIPlan.id == AIPlanDay.plan_id)
        .filter(AIPlanStep.id == step_id, AIPlan.user_id == user_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if row is None:
        if db.query(AIPlanStep.id).filter(AIPlanStep.id == step_id).first() is not None:
            raise LifecycleOwnershipError("plan_step_not_owned")
        raise LifecycleTransitionError("plan_step_missing")
    step, plan = row
    if str(plan.status) != "active":
        raise LifecycleTransitionError("plan_not_active")

    current = str(step.step_status)
    if current in TERMINAL_STEP_STATUSES:
        if current != target_status:
            raise LifecycleTransitionError(f"terminal step already won with {current}")
        record_lifecycle_operation(
            db,
            user_id=user_id,
            plan_id=plan.id,
            step_id=step.id,
            source_operation_id=source_operation_id,
            operation=expected_operation,
            result_status=current,
        )
        db.flush()
        result = LifecycleResult(
            user_id=user_id,
            plan_id=plan.id,
            step_id=step.id,
            status=current,
            operation=f"step_{target_status}",
            duplicate=True,
            day_number=step.day.day_number,
        )
        if target_status == "expired":
            target_ids = (step.id,) if step.tg_message_id else ()
            return replace(
                result,
                effects=(
                    ExternalEffect(
                        kind="remove_step_keyboard",
                        target_ids=target_ids,
                        state=(
                            ExternalEffectState.PENDING
                            if target_ids
                            else ExternalEffectState.NOT_REQUIRED
                        ),
                    ),
                ),
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
        user_id=user_id,
        plan_id=plan.id,
        step_id=step.id,
        status=target_status,
        operation=expected_operation,
        duplicate=current == target_status,
        day_number=step.day.day_number,
        effects=(
            ExternalEffect(
                kind="remove_step_keyboard",
                target_ids=(step.id,) if step.tg_message_id else (),
                state=(
                    ExternalEffectState.PENDING
                    if target_status == "expired" and step.tg_message_id
                    else ExternalEffectState.NOT_REQUIRED
                ),
            ),
        )
        if target_status == "expired"
        else (),
    )


def transition_owned_plan_step(
    db: Session,
    *,
    telegram_user_id: int,
    step_id: int,
    target_status: str,
    source_operation_id: str,
    occurred_at: datetime | None = None,
) -> LifecycleResult:
    """Resolve a Telegram actor, then enforce aggregate ownership in one boundary."""
    actor = _lock_telegram_actor(db, telegram_user_id)
    return transition_plan_step(
        db,
        user_id=actor.id,
        step_id=step_id,
        target_status=target_status,
        source_operation_id=source_operation_id,
        occurred_at=occurred_at,
    )


def expire_plan_step(
    db: Session,
    *,
    user_id: int,
    step_id: int,
    source_operation_id: str,
    occurred_at: datetime,
) -> LifecycleResult:
    """Expire and record ignored telemetry as one committed lifecycle fact."""
    result = transition_plan_step(
        db,
        user_id=user_id,
        step_id=step_id,
        target_status="expired",
        source_operation_id=source_operation_id,
        occurred_at=occurred_at,
    )
    if not result.duplicate:
        from app.telemetry import write_event_operation

        write_event_operation(
            db,
            user_id=user_id,
            event_name="task_ignored",
            event_source="scheduler",
            source_operation_id=source_operation_id,
            plan_step_id=step_id,
            occurred_at=occurred_at,
            properties={"detection_source": "local_expiry"},
        )
    return result


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
        duplicate = _duplicate_operation_result(
            existing,
            expected_operation="complete",
            expected_plan_id=plan_id,
        )
        completed_plan = (
            db.query(AIPlan)
            .filter(AIPlan.id == existing.plan_id, AIPlan.user_id == user_id)
            .populate_existing()
            .first()
        )
        if completed_plan is None:
            raise LifecycleInvariantError("completion_receipt_plan_missing")
        return replace(
            duplicate,
            plan_type=_plan_type(completed_plan),
            effects=(
                ExternalEffect(
                    kind="send_completion_report",
                    target_ids=(completed_plan.id,),
                ),
            ),
            details={"continuation": "deferred_to_wp_03_5"},
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
    return LifecycleResult(
        user_id=user_id,
        plan_id=plan.id,
        status="completed",
        operation="complete",
        plan_type=_plan_type(plan),
        effects=(
            ExternalEffect(
                kind="send_completion_report",
                target_ids=(plan.id,),
            ),
        ),
        details={"continuation": "deferred_to_wp_03_5"},
    )


def _lifecycle_context_plan(db: Session, user_id: int) -> AIPlan:
    current = get_current_plan(db, user_id, lock=True)
    if current is not None:
        return current
    latest = (
        db.query(AIPlan)
        .filter(AIPlan.user_id == user_id)
        .populate_existing()
        .order_by(AIPlan.cycle_number.desc(), AIPlan.id.desc())
        .with_for_update()
        .first()
    )
    if latest is None:
        raise LifecycleTransitionError("lifecycle_context_missing")
    return latest


def _pending_step_ids(db: Session, plan_id: int) -> tuple[int, ...]:
    return tuple(
        step_id
        for (step_id,) in (
            db.query(AIPlanStep.id)
            .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
            .filter(
                AIPlanDay.plan_id == plan_id,
                AIPlanStep.step_status == "pending",
            )
            .order_by(AIPlanStep.id)
            .all()
        )
    )


def change_delivery_time(
    db: Session,
    *,
    user_id: int,
    slot: str,
    hhmm: str,
    source_operation_id: str,
) -> LifecycleResult:
    """Persist one time change and expose post-commit scheduler reconciliation."""
    normalized_slot = str(slot).strip().upper()
    if normalized_slot not in {"MORNING", "DAY", "EVENING"}:
        raise LifecycleTransitionError("unsupported_delivery_slot")
    operation = f"change_{normalized_slot.lower()}_time"
    user = _lock_user(db, user_id)

    existing = find_lifecycle_operation(db, user_id, source_operation_id)
    if existing is not None:
        duplicate = _duplicate_operation_result(
            existing,
            expected_operation=operation,
            expected_result_status=hhmm,
        )
        context_plan = (
            db.query(AIPlan)
            .filter(AIPlan.id == existing.plan_id, AIPlan.user_id == user_id)
            .populate_existing()
            .first()
        )
        if context_plan is None:
            raise LifecycleInvariantError("schedule_receipt_plan_missing")
        from app.time_slots import resolve_daily_time_slots

        current_value = resolve_daily_time_slots(user.profile).get(normalized_slot)
        still_authoritative = current_value == hhmm
        active_ids = (
            _pending_step_ids(db, context_plan.id)
            if still_authoritative and str(context_plan.status) == "active"
            else ()
        )
        state = (
            ExternalEffectState.PENDING
            if active_ids
            else (
                ExternalEffectState.DEFERRED
                if still_authoritative and str(context_plan.status) == "paused"
                else ExternalEffectState.NOT_REQUIRED
            )
        )
        return replace(
            duplicate,
            plan_type=_plan_type(context_plan),
            code="replayed" if still_authoritative else "superseded",
            effects=(
                ExternalEffect(
                    kind="reconcile_plan_schedule",
                    target_ids=active_ids,
                    state=state,
                ),
            ),
            details={
                "slot": normalized_slot,
                "value": hhmm,
                "authoritative_value": current_value,
            },
        )

    context_plan = _lifecycle_context_plan(db, user_id)
    from app.time_slots import TimeSlotError, update_user_time_slots

    try:
        updated_ids, active_ids = update_user_time_slots(
            db,
            user,
            {normalized_slot: hhmm},
        )
    except TimeSlotError as exc:
        raise LifecycleTransitionError(str(exc)) from exc
    pending_ids = set(_pending_step_ids(db, context_plan.id))
    active_ids = [step_id for step_id in active_ids if step_id in pending_ids]
    record_lifecycle_operation(
        db,
        user_id=user_id,
        plan_id=context_plan.id,
        source_operation_id=source_operation_id,
        operation=operation,
        result_status=hhmm,
    )
    db.flush()
    state = (
        ExternalEffectState.PENDING
        if active_ids
        else (
            ExternalEffectState.DEFERRED
            if str(context_plan.status) == "paused"
            else ExternalEffectState.NOT_REQUIRED
        )
    )
    return LifecycleResult(
        user_id=user_id,
        plan_id=context_plan.id,
        status=hhmm,
        operation=operation,
        plan_type=_plan_type(context_plan),
        effects=(
            ExternalEffect(
                kind="reconcile_plan_schedule",
                target_ids=tuple(active_ids),
                state=state,
            ),
        ),
        details={
            "slot": normalized_slot,
            "value": hhmm,
            "updated_step_ids": tuple(updated_ids),
        },
    )


def record_evening_time_preference(
    db: Session,
    *,
    user_id: int,
    hhmm: str,
    source_operation_id: str,
) -> LifecycleResult:
    """Idempotently persist the collected MEDIUM-plan evening preference."""
    operation = "record_evening_time"
    user = _lock_user(db, user_id)
    existing = find_lifecycle_operation(db, user_id, source_operation_id)
    if existing is not None:
        duplicate = _duplicate_operation_result(
            existing,
            expected_operation=operation,
            expected_result_status=hhmm,
        )
        from app.time_slots import resolve_daily_time_slots

        current_value = resolve_daily_time_slots(user.profile).get("EVENING")
        return replace(
            duplicate,
            code="replayed" if current_value == hhmm else "superseded",
            details={
                "value": hhmm,
                "authoritative_value": current_value,
                "collected": bool(
                    user.profile and user.profile.evening_slot_collected
                ),
            },
        )

    context_plan = _lifecycle_context_plan(db, user_id)
    from app.time_slots import TimeSlotError, update_user_time_slots

    try:
        update_user_time_slots(db, user, {"EVENING": hhmm})
    except TimeSlotError as exc:
        raise LifecycleTransitionError(str(exc)) from exc
    if user.profile is None:
        raise LifecycleInvariantError("user_profile_missing_after_time_update")
    user.profile.evening_slot_collected = True
    record_lifecycle_operation(
        db,
        user_id=user_id,
        plan_id=context_plan.id,
        source_operation_id=source_operation_id,
        operation=operation,
        result_status=hhmm,
    )
    db.flush()
    return LifecycleResult(
        user_id=user_id,
        plan_id=context_plan.id,
        status=hhmm,
        operation=operation,
        plan_type=_plan_type(context_plan),
        details={"value": hhmm, "collected": True},
    )


def request_plan_format_switch(
    db: Session,
    *,
    user_id: int,
    target_plan_type: str,
    source_operation_id: str,
) -> LifecycleResult:
    """Stable WP-02.3 interface; no format mutation is implemented here."""
    if target_plan_type not in {"SHORT", "MEDIUM"}:
        raise LifecycleTransitionError("unsupported_plan_type")
    if not source_operation_id or len(source_operation_id) > 160:
        raise LifecycleTransitionError("invalid_source_operation_id")
    _lock_user(db, user_id)
    plan = get_current_plan(db, user_id, lock=True)
    if plan is None:
        raise LifecycleTransitionError("current_plan_missing")
    return LifecycleResult(
        user_id=user_id,
        plan_id=plan.id,
        status=str(plan.status),
        operation="switch_plan_format",
        code="deferred_to_wp_02_3",
        applied=False,
        plan_type=_plan_type(plan),
        details={"target_plan_type": target_plan_type},
    )


def prepare_continuation(
    db: Session,
    *,
    user_id: int,
    completed_plan_id: int,
    source_operation_id: str,
) -> ContinuationInterfaceResult:
    """Validate the completed source plan without creating its continuation."""
    if not source_operation_id or len(source_operation_id) > 160:
        raise LifecycleTransitionError("invalid_source_operation_id")
    _lock_user(db, user_id)
    plan = (
        db.query(AIPlan)
        .filter(AIPlan.id == completed_plan_id, AIPlan.user_id == user_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if plan is None:
        if db.query(AIPlan.id).filter(AIPlan.id == completed_plan_id).first() is not None:
            raise LifecycleOwnershipError("plan_not_owned")
        raise LifecycleTransitionError("plan_missing")
    if str(plan.status) != "completed":
        raise LifecycleTransitionError("continuation_requires_completed_plan")
    return ContinuationInterfaceResult(
        user_id=user_id,
        completed_plan_id=plan.id,
        plan_type=_plan_type(plan),
        source_operation_id=source_operation_id,
    )


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

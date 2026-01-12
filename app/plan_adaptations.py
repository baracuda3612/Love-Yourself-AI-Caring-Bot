"""Plan adaptation application service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from sqlalchemy.orm import Session, selectinload

from app.db import AIPlan, AIPlanDay, AIPlanStep, AIPlanVersion
from app.telemetry import log_user_event
from app.time_slots import (
    compute_scheduled_for,
    normalize_time_slot,
    resolve_daily_time_slots,
    resolve_step_date,
)

_ALLOWED_ADAPTATION_TYPES = {"reduce_load", "shift_timing", "pause", "resume"}


class PlanAdaptationError(ValueError):
    """Raised when a plan adaptation payload is invalid or cannot be applied."""


@dataclass
class PlanAdaptationResult:
    plan_id: int
    user_id: int
    adaptation_type: str
    scope: str
    step_diff_count: int
    canceled_step_ids: List[int]
    rescheduled_step_ids: List[int]


def _parse_effective_from(raw_value: Any) -> datetime:
    if not raw_value:
        return datetime.now(timezone.utc)
    if isinstance(raw_value, datetime):
        value = raw_value
    else:
        try:
            value = datetime.fromisoformat(str(raw_value))
        except ValueError as exc:
            raise PlanAdaptationError("invalid_effective_from") from exc
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _resolve_step_anchor(plan: AIPlan, day: AIPlanDay, step: AIPlanStep) -> datetime:
    if step.scheduled_for:
        return step.scheduled_for.astimezone(timezone.utc)
    start_date = plan.start_date or datetime.now(timezone.utc)
    if start_date.tzinfo is None:
        start_date = start_date.replace(tzinfo=timezone.utc)
    anchor = start_date + timedelta(days=max(day.day_number - 1, 0))
    return anchor.astimezone(timezone.utc)


def _iter_future_steps(
    plan: AIPlan,
    effective_from: datetime,
) -> Iterable[Tuple[AIPlanDay, AIPlanStep]]:
    for day in plan.days:
        for step in day.steps:
            if step.is_completed or step.skipped:
                continue
            if _resolve_step_anchor(plan, day, step) >= effective_from:
                yield day, step


def _resolve_daily_target(params: Dict[str, Any], default_target: Optional[int]) -> Optional[int]:
    for key in ("daily_step_target", "max_steps_per_day", "steps_per_day", "daily_steps"):
        raw_value = params.get(key)
        if raw_value is None:
            continue
        try:
            parsed = int(raw_value)
        except (TypeError, ValueError):
            raise PlanAdaptationError("invalid_daily_step_target")
        return max(1, parsed)
    return default_target


def _apply_reduce_load(
    plan: AIPlan,
    effective_from: datetime,
    params: Dict[str, Any],
) -> Tuple[int, List[int]]:
    step_diff_count = 0
    skipped_step_ids: List[int] = []
    for day in plan.days:
        future_steps = [
            step
            for day_ref, step in _iter_future_steps(plan, effective_from)
            if day_ref.id == day.id
        ]
        if not future_steps:
            continue
        target = _resolve_daily_target(params, None)
        if target is None:
            target = max(1, len(future_steps) - 1)
        if len(future_steps) <= target:
            continue
        future_steps.sort(key=lambda step: step.order_in_day)
        for step in future_steps[target:]:
            step.skipped = True
            step.scheduled_for = None
            step_diff_count += 1
            skipped_step_ids.append(step.id)
    return step_diff_count, skipped_step_ids


def _apply_shift_timing(
    plan: AIPlan,
    effective_from: datetime,
    params: Dict[str, Any],
) -> Tuple[int, List[int]]:
    changed_step_ids: List[int] = []
    raw_time_slot = params.get("time_slot")
    if not raw_time_slot:
        return 0, []
    try:
        time_slot = normalize_time_slot(raw_time_slot)
    except ValueError as exc:
        raise PlanAdaptationError("invalid_time_slot") from exc
    daily_time_slots = resolve_daily_time_slots(plan.user.profile if plan.user else None)
    for day, step in _iter_future_steps(plan, effective_from):
        plan_start = plan.start_date or effective_from
        anchor_date = resolve_step_date(
            plan_start=plan_start,
            day_number=day.day_number,
            scheduled_for=step.scheduled_for,
            timezone_name=plan.user.timezone if plan.user else None,
        )
        step.time_slot = time_slot
        step.scheduled_for = compute_scheduled_for(
            plan_start=plan_start,
            day_number=day.day_number,
            time_slot=time_slot,
            timezone_name=plan.user.timezone if plan.user else None,
            daily_time_slots=daily_time_slots,
            anchor_date=anchor_date,
        )
        changed_step_ids.append(step.id)
    return len(changed_step_ids), changed_step_ids


def _apply_pause(
    plan: AIPlan,
    effective_from: datetime,
) -> Tuple[int, List[int]]:
    affected_steps = [step.id for _, step in _iter_future_steps(plan, effective_from)]
    plan.execution_policy = "paused"
    return len(affected_steps), affected_steps


def _apply_resume(
    plan: AIPlan,
    effective_from: datetime,
) -> Tuple[int, List[int]]:
    plan.execution_policy = "active"
    rescheduled_step_ids: List[int] = []
    daily_time_slots = resolve_daily_time_slots(plan.user.profile if plan.user else None)
    for day, step in _iter_future_steps(plan, effective_from):
        plan_start = plan.start_date or effective_from
        anchor_date = resolve_step_date(
            plan_start=plan_start,
            day_number=day.day_number,
            scheduled_for=step.scheduled_for,
            timezone_name=plan.user.timezone if plan.user else None,
        )
        step.time_slot = normalize_time_slot(step.time_slot)
        step.scheduled_for = compute_scheduled_for(
            plan_start=plan_start,
            day_number=day.day_number,
            time_slot=step.time_slot,
            timezone_name=plan.user.timezone if plan.user else None,
            daily_time_slots=daily_time_slots,
            anchor_date=anchor_date,
        )
        rescheduled_step_ids.append(step.id)
    return len(rescheduled_step_ids), rescheduled_step_ids


def apply_plan_adaptation(
    db: Session,
    plan_id: int,
    adaptation_payload: Dict[str, Any],
) -> PlanAdaptationResult:
    adaptation_type = adaptation_payload.get("adaptation_type")
    if adaptation_type not in _ALLOWED_ADAPTATION_TYPES:
        raise PlanAdaptationError("unsupported_adaptation_type")

    effective_from = _parse_effective_from(adaptation_payload.get("effective_from"))
    params = adaptation_payload.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise PlanAdaptationError("invalid_params")

    plan = (
        db.query(AIPlan)
        .options(selectinload(AIPlan.days).selectinload(AIPlanDay.steps), selectinload(AIPlan.user))
        .filter(AIPlan.id == plan_id)
        .first()
    )
    if not plan:
        raise PlanAdaptationError("plan_not_found")

    step_diff_count = 0
    canceled_step_ids: List[int] = []
    rescheduled_step_ids: List[int] = []
    scope = adaptation_type

    if adaptation_type == "reduce_load":
        step_diff_count, canceled_step_ids = _apply_reduce_load(plan, effective_from, params)
    elif adaptation_type == "shift_timing":
        step_diff_count, rescheduled_step_ids = _apply_shift_timing(plan, effective_from, params)
        canceled_step_ids = list(rescheduled_step_ids)
    elif adaptation_type == "pause":
        step_diff_count, canceled_step_ids = _apply_pause(plan, effective_from)
        if plan.user:
            log_user_event(
                db,
                plan.user.id,
                "plan_paused",
                context={"adaptation_type": adaptation_type, "effective_from": effective_from.isoformat()},
            )
    elif adaptation_type == "resume":
        step_diff_count, rescheduled_step_ids = _apply_resume(plan, effective_from)
        if plan.user:
            log_user_event(
                db,
                plan.user.id,
                "plan_resumed",
                context={"adaptation_type": adaptation_type, "effective_from": effective_from.isoformat()},
            )

    db.add(
        AIPlanVersion(
            plan_id=plan.id,
            applied_adaptation_type=adaptation_type,
            diff={
                "effective_from": effective_from.isoformat(),
                "params": params,
                "step_diff_count": step_diff_count,
                "canceled_step_ids": canceled_step_ids,
                "rescheduled_step_ids": rescheduled_step_ids,
                "execution_policy": plan.execution_policy,
            },
        )
    )

    return PlanAdaptationResult(
        plan_id=plan.id,
        user_id=plan.user_id,
        adaptation_type=adaptation_type,
        scope=scope,
        step_diff_count=step_diff_count,
        canceled_step_ids=canceled_step_ids,
        rescheduled_step_ids=rescheduled_step_ids,
    )

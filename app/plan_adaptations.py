"""Plan adaptation application service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pytz
from sqlalchemy.orm import Session, selectinload

from app.db import AIPlan, AIPlanDay, AIPlanStep, AIPlanVersion
from app.telemetry import log_user_event

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


def _normalize_timezone(name: Optional[str]) -> pytz.BaseTzInfo:
    try:
        return pytz.timezone(name or "UTC")
    except pytz.UnknownTimeZoneError:
        return pytz.UTC


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


def _parse_time(value: str | None) -> Optional[time]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    parts = text.split(":", 1)
    if len(parts) != 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return time(hour=hour, minute=minute)
    return None


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
    time_of_day = params.get("time_of_day")
    scheduled_map = params.get("scheduled_for_by_step_id") or params.get("scheduled_for_map")
    raw_time_override = params.get("scheduled_time") or params.get("scheduled_for")
    tz = _normalize_timezone(plan.user.timezone if plan.user else None)

    scheduled_time = _parse_time(str(raw_time_override)) if raw_time_override else None
    scheduled_datetime: Optional[datetime] = None
    if raw_time_override and scheduled_time is None:
        try:
            scheduled_datetime = datetime.fromisoformat(str(raw_time_override))
        except ValueError:
            scheduled_datetime = None

    for day, step in _iter_future_steps(plan, effective_from):
        changed = False
        if time_of_day:
            step.time_of_day = str(time_of_day)
            changed = True
        if isinstance(scheduled_map, dict):
            raw_value = scheduled_map.get(str(step.id))
            if raw_value:
                try:
                    parsed_dt = datetime.fromisoformat(str(raw_value))
                except ValueError as exc:
                    raise PlanAdaptationError("invalid_scheduled_for_map") from exc
                if parsed_dt.tzinfo is None:
                    parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
                step.scheduled_for = parsed_dt.astimezone(timezone.utc)
                changed = True
        elif scheduled_time is not None or scheduled_datetime is not None:
            base_time = scheduled_time
            if base_time is None and scheduled_datetime is not None:
                base_time = scheduled_datetime.timetz().replace(tzinfo=None)
            if base_time is not None:
                day_start = plan.start_date or datetime.now(timezone.utc)
                if day_start.tzinfo is None:
                    day_start = day_start.replace(tzinfo=timezone.utc)
                target_date = (day_start + timedelta(days=max(day.day_number - 1, 0))).date()
                naive_local = datetime.combine(target_date, base_time)
                try:
                    localized = tz.localize(naive_local)
                except pytz.NonExistentTimeError:
                    localized = tz.localize(naive_local + timedelta(hours=1))
                except pytz.AmbiguousTimeError:
                    localized = tz.localize(naive_local, is_dst=False)
                step.scheduled_for = localized.astimezone(timezone.utc)
                changed = True
        if changed:
            changed_step_ids.append(step.id)
    return len(changed_step_ids), changed_step_ids


def _apply_pause_or_resume(
    plan: AIPlan,
    effective_from: datetime,
    new_policy: str,
) -> Tuple[int, List[int]]:
    affected_steps = [step.id for _, step in _iter_future_steps(plan, effective_from)]
    plan.execution_policy = new_policy
    return len(affected_steps), affected_steps


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
        step_diff_count, canceled_step_ids = _apply_pause_or_resume(plan, effective_from, "paused")
        if plan.user:
            log_user_event(
                db,
                plan.user.id,
                "plan_paused",
                context={"adaptation_type": adaptation_type, "effective_from": effective_from.isoformat()},
            )
    elif adaptation_type == "resume":
        step_diff_count, rescheduled_step_ids = _apply_pause_or_resume(plan, effective_from, "active")
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

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.adaptation_metrics import get_adaptation_count
from app.db import AIPlan
from app.plan_metrics import fetch_delivered_steps


@dataclass(frozen=True)
class CompletionMetrics:
    plan_id: int
    total_days: int
    total_delivered: int
    total_completed: int
    total_skipped: int
    total_ignored: int
    completion_rate: float
    best_streak: int
    had_adaptations: bool
    adaptation_count: int
    dominant_time_slot: str | None
    focus: str | None
    load: str | None
    duration: str | None
    outcome_tier: str


def _compute_best_streak(completed_day_numbers: set[int]) -> int:
    if not completed_day_numbers:
        return 0

    best = 0
    current = 0
    previous = None

    for day_number in sorted(completed_day_numbers):
        if previous is None or day_number == previous + 1:
            current += 1
        else:
            current = 1
        best = max(best, current)
        previous = day_number

    return best


def _resolve_outcome_tier(completion_rate: float) -> str:
    if completion_rate >= 0.80:
        return "STRONG"
    if completion_rate >= 0.50:
        return "NEUTRAL"
    return "WEAK"


def build_completion_metrics(
    db: Session,
    user_id: int,
    plan_id: int,
) -> CompletionMetrics:
    """
    Збирає повну статистику завершеного плану.
    Чиста функція — тільки reads, ніяких side effects.
    """
    plan = (
        db.query(AIPlan)
        .filter(AIPlan.id == plan_id, AIPlan.user_id == user_id)
        .first()
    )
    if plan is None:
        raise ValueError(f"Plan {plan_id} not found for user {user_id}")

    delivered = fetch_delivered_steps(db, user_id, plan_id)
    total_delivered = len(delivered)

    adaptation_count = get_adaptation_count(db, plan_id)

    if total_delivered == 0:
        return CompletionMetrics(
            plan_id=plan.id,
            total_days=plan.total_days or 0,
            total_delivered=0,
            total_completed=0,
            total_skipped=0,
            total_ignored=0,
            completion_rate=0.0,
            best_streak=0,
            had_adaptations=adaptation_count > 0,
            adaptation_count=adaptation_count,
            dominant_time_slot=None,
            focus=plan.focus,
            load=plan.load,
            duration=plan.duration,
            outcome_tier="WEAK",
        )

    total_completed = sum(1 for step, _timestamp in delivered if step.is_completed)
    total_skipped = sum(1 for step, _timestamp in delivered if step.skipped)
    total_ignored = max(total_delivered - total_completed - total_skipped, 0)
    completion_rate = total_completed / total_delivered

    completed_day_numbers = {
        step.day.day_number
        for step, _timestamp in delivered
        if step.is_completed and step.day is not None
    }
    best_streak = _compute_best_streak(completed_day_numbers)

    slot_counts: dict[str, int] = {}
    for step, _timestamp in delivered:
        if not step.is_completed or not step.time_slot:
            continue
        slot_counts[step.time_slot] = slot_counts.get(step.time_slot, 0) + 1

    dominant_time_slot: str | None = None
    if slot_counts:
        max_count = max(slot_counts.values())
        leaders = [slot for slot, count in slot_counts.items() if count == max_count]
        if len(leaders) == 1:
            dominant_time_slot = leaders[0]

    return CompletionMetrics(
        plan_id=plan.id,
        total_days=plan.total_days or 0,
        total_delivered=total_delivered,
        total_completed=total_completed,
        total_skipped=total_skipped,
        total_ignored=total_ignored,
        completion_rate=completion_rate,
        best_streak=best_streak,
        had_adaptations=adaptation_count > 0,
        adaptation_count=adaptation_count,
        dominant_time_slot=dominant_time_slot,
        focus=plan.focus,
        load=plan.load,
        duration=plan.duration,
        outcome_tier=_resolve_outcome_tier(completion_rate),
    )

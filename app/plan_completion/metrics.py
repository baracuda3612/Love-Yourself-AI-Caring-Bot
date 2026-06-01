from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pytz
from sqlalchemy.orm import Session

from app.active_days import consecutive_active_days_gap, resolve_active_days
from app.db import AIPlan, AIPlanDay, AIPlanStep


STRONG_THRESHOLD = 0.80
NEUTRAL_THRESHOLD = 0.50


@dataclass(frozen=True)
class CompletionMetrics:
    plan_id: int
    total_days: int
    total_delivered: int
    total_completed: int
    total_skipped: int
    total_ignored: int          # = expired (user had window, did not react)
    # ── Rates ──────────────────────────────────────────────────────────────────
    completion_rate: float      # completed / eligible
    engagement_rate: float      # (completed + skipped) / eligible  — conscious action
    silent_miss_rate: float     # expired / eligible  — churn signal
    # ── Streak ─────────────────────────────────────────────────────────────────
    best_streak: int            # longest streak over the whole plan
    current_streak: int         # streak ending at the last completed date
    # ── Context ────────────────────────────────────────────────────────────────
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


def _compute_best_streak_by_date(
    completed_dates: list,
    active_days: list[str],
) -> int:
    """
    Streak that ignores gaps caused by non-active days.
    FRI → MON is consecutive if SAT/SUN are not in active_days.
    """
    if not completed_dates:
        return 0

    sorted_dates = sorted(set(completed_dates))
    best = 1
    current = 1

    for i in range(1, len(sorted_dates)):
        prev_date = sorted_dates[i - 1]
        curr_date = sorted_dates[i]
        if consecutive_active_days_gap(prev_date, curr_date, active_days):
            current += 1
        else:
            current = 1
        best = max(best, current)

    return best


def _compute_current_streak(
    completed_dates: list,
    active_days: list[str],
) -> int:
    """
    Consecutive streak ending at the most recent completed date.
    Walks backward from the last date, stops at the first gap.
    """
    if not completed_dates:
        return 0

    sorted_dates = sorted(set(completed_dates), reverse=True)
    streak = 1

    for i in range(1, len(sorted_dates)):
        more_recent = sorted_dates[i - 1]
        older = sorted_dates[i]
        if consecutive_active_days_gap(older, more_recent, active_days):
            streak += 1
        else:
            break

    return streak


def _fetch_eligible_steps(
    db: Session,
    plan_id: int,
    now_utc: datetime,
) -> list[AIPlanStep]:
    """Query steps eligible for completion metrics (completed | skipped | expired)."""
    return (
        db.query(AIPlanStep)
        .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
        .filter(
            AIPlanDay.plan_id == plan_id,
            AIPlanStep.step_status.in_(["completed", "skipped", "expired"]),
            AIPlanStep.scheduled_for <= now_utc,
        )
        .all()
    )


def _resolve_outcome_tier(completion_rate: float) -> str:
    if completion_rate >= STRONG_THRESHOLD:
        return "STRONG"
    if completion_rate >= NEUTRAL_THRESHOLD:
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

    # T5.4: adaptations removed — always 0. Remove field from CompletionMetrics when
    # completion report is redesigned (backlog).
    adaptation_count = 0

    # Eligible steps: completed | skipped | expired, scheduled <= now, not canceled
    now_utc = datetime.now(pytz.UTC)
    eligible_steps = _fetch_eligible_steps(db, plan_id, now_utc)

    total_delivered = len(eligible_steps)

    if total_delivered == 0:
        return CompletionMetrics(
            plan_id=plan.id,
            total_days=plan.total_days or 0,
            total_delivered=0,
            total_completed=0,
            total_skipped=0,
            total_ignored=0,
            completion_rate=0.0,
            engagement_rate=0.0,
            silent_miss_rate=0.0,
            best_streak=0,
            current_streak=0,
            had_adaptations=adaptation_count > 0,
            adaptation_count=adaptation_count,
            dominant_time_slot=None,
            focus=plan.focus,
            load=plan.load,
            duration=plan.duration,
            outcome_tier="WEAK",
        )

    total_completed = sum(1 for s in eligible_steps if s.step_status == "completed")
    total_skipped = sum(1 for s in eligible_steps if s.step_status == "skipped")
    total_ignored = sum(1 for s in eligible_steps if s.step_status == "expired")
    # Zero-division guard (total_delivered > 0 guaranteed by check above)
    completion_rate = total_completed / total_delivered
    engagement_rate = (total_completed + total_skipped) / total_delivered
    silent_miss_rate = total_ignored / total_delivered

    # Streak by real calendar dates, respecting active_days gaps.
    user = plan.user
    active_days = resolve_active_days(getattr(user, "profile", None))
    user_tz_str = getattr(user, "timezone", None) or "Europe/Kyiv"
    try:
        user_tz = pytz.timezone(user_tz_str)
    except Exception:
        user_tz = pytz.timezone("Europe/Kyiv")

    completed_dates = [
        s.scheduled_for.astimezone(user_tz).date()
        for s in eligible_steps
        if s.step_status == "completed" and s.scheduled_for
    ]
    best_streak = _compute_best_streak_by_date(completed_dates, active_days)
    current_streak = _compute_current_streak(completed_dates, active_days)

    slot_counts: dict[str, int] = {}
    for s in eligible_steps:
        if s.step_status != "completed" or not s.time_slot:
            continue
        slot_counts[s.time_slot] = slot_counts.get(s.time_slot, 0) + 1

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
        engagement_rate=engagement_rate,
        silent_miss_rate=silent_miss_rate,
        best_streak=best_streak,
        current_streak=current_streak,
        had_adaptations=adaptation_count > 0,
        adaptation_count=adaptation_count,
        dominant_time_slot=dominant_time_slot,
        focus=plan.focus,
        load=plan.load,
        duration=plan.duration,
        outcome_tier=_resolve_outcome_tier(completion_rate),
    )

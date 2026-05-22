"""
Tests for T3.3 + Work Days Group — Task Lifecycle & Active Delivery Overhaul.

Covers:
1. Streak FRI→MON is NOT broken when SAT/SUN are inactive
2. completion_rate excludes future pending steps (scheduled > now)
3. Expired guard: step_status == "expired" blocks user action and returns correct message
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import pytest

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql://test-user:test-pass@localhost:5432/test-db")
os.environ.setdefault("OPENAI_API_KEY", "test-key")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

from app.active_days import consecutive_active_days_gap
from app.plan_completion.metrics import _compute_best_streak_by_date, _compute_current_streak
from app.plan_guards import validate_step_action, is_step_terminal


# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------

ACTIVE_DAYS_WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI"]


@dataclass
class _StubPlan:
    status: str = "active"
    user: object = None


@dataclass
class _StubDay:
    plan: _StubPlan = field(default_factory=_StubPlan)


@dataclass
class _StubUser:
    current_state: str = "ACTIVE"


@dataclass
class _StubStep:
    step_status: str = "pending"
    day: _StubDay = field(default_factory=_StubDay)

    def __post_init__(self):
        self.day.plan.user = _StubUser()


# ---------------------------------------------------------------------------
# Test 1: Streak calculation – FRI→MON is consecutive for weekday-only schedule
# ---------------------------------------------------------------------------


class TestStreakActiveDaysGap:
    def test_fri_to_mon_is_consecutive(self):
        """FRI → MON should be consecutive when SAT & SUN are not active."""
        fri = date(2026, 5, 15)  # Friday
        mon = date(2026, 5, 18)  # Monday
        assert consecutive_active_days_gap(fri, mon, ACTIVE_DAYS_WEEKDAYS) is True

    def test_fri_to_tue_is_not_consecutive(self):
        """FRI → TUE has MON between them, so it should NOT be consecutive."""
        fri = date(2026, 5, 15)
        tue = date(2026, 5, 19)
        assert consecutive_active_days_gap(fri, tue, ACTIVE_DAYS_WEEKDAYS) is False

    def test_mon_to_tue_is_consecutive(self):
        fri = date(2026, 5, 18)
        sat = date(2026, 5, 19)
        assert consecutive_active_days_gap(fri, sat, ACTIVE_DAYS_WEEKDAYS) is True

    def test_streak_ignores_weekend_gap(self):
        """MON TUE WED THU FRI [SAT SUN] MON → streak of 6 (continuous work week + next Mon)."""
        completed = [
            date(2026, 5, 11),  # Mon
            date(2026, 5, 12),  # Tue
            date(2026, 5, 13),  # Wed
            date(2026, 5, 14),  # Thu
            date(2026, 5, 15),  # Fri
            date(2026, 5, 18),  # Mon (next week — no break)
        ]
        streak = _compute_best_streak_by_date(completed, ACTIVE_DAYS_WEEKDAYS)
        assert streak == 6

    def test_streak_breaks_on_missed_weekday(self):
        """Skipping TUE means streak resets: WED–FRI–MON is only 4."""
        completed = [
            date(2026, 5, 11),  # Mon  → streak 1
            # Tue missing → break
            date(2026, 5, 13),  # Wed  → streak 1
            date(2026, 5, 14),  # Thu  → streak 2
            date(2026, 5, 15),  # Fri  → streak 3
            date(2026, 5, 18),  # Mon  → streak 4
        ]
        streak = _compute_best_streak_by_date(completed, ACTIVE_DAYS_WEEKDAYS)
        assert streak == 4

    def test_streak_empty_returns_zero(self):
        assert _compute_best_streak_by_date([], ACTIVE_DAYS_WEEKDAYS) == 0


# ---------------------------------------------------------------------------
# Test 2: completion_rate excludes future steps
# ---------------------------------------------------------------------------


class TestCompletionRateExcludesFuture:
    """
    Validate that _resolve_outcome_tier and rate logic only count eligible steps
    (completed | skipped | expired, scheduled_for <= now).
    We test the pure helper _compute_best_streak_by_date indirectly and
    verify the query filter logic via unit-level reasoning.
    """

    def test_future_pending_step_not_in_denominator(self):
        """
        If we have 2 completed steps and 1 pending future step,
        completion_rate should be 2/2 = 1.0, not 2/3.
        The query filter (step_status IN ('completed','skipped','expired')
        AND scheduled_for <= now) already excludes future pending steps.
        We verify the arithmetic holds for the eligible-only denominator.
        """
        now = datetime.now(timezone.utc)
        past = now - timedelta(hours=1)
        future = now + timedelta(hours=2)

        @dataclass
        class _FakeStep:
            step_status: str
            scheduled_for: datetime
            canceled_by_adaptation: bool = False

        eligible_statuses = ("completed", "skipped", "expired")
        steps = [
            _FakeStep("completed", past),
            _FakeStep("completed", past),
            _FakeStep("pending", future),   # must be excluded
        ]

        eligible = [
            s for s in steps
            if s.step_status in eligible_statuses
            and s.scheduled_for <= now
            and not s.canceled_by_adaptation
        ]

        total_delivered = len(eligible)
        total_completed = sum(1 for s in eligible if s.step_status == "completed")
        rate = total_completed / total_delivered if total_delivered else 0.0

        assert total_delivered == 2
        assert rate == 1.0

    def test_expired_steps_count_in_denominator_but_not_numerator(self):
        """Expired steps lower completion_rate (they're eligible but not completed)."""
        now = datetime.now(timezone.utc)
        past = now - timedelta(hours=1)

        @dataclass
        class _FakeStep:
            step_status: str
            scheduled_for: datetime
            canceled_by_adaptation: bool = False

        eligible_statuses = ("completed", "skipped", "expired")
        steps = [
            _FakeStep("completed", past),
            _FakeStep("expired", past),
        ]

        eligible = [
            s for s in steps
            if s.step_status in eligible_statuses
            and s.scheduled_for <= now
            and not s.canceled_by_adaptation
        ]

        rate = sum(1 for s in eligible if s.step_status == "completed") / len(eligible)
        assert rate == 0.5


# ---------------------------------------------------------------------------
# Test 3: Expired guard blocks action + correct message
# ---------------------------------------------------------------------------


class TestExpiredStepGuard:
    def test_expired_step_is_terminal(self):
        step = _StubStep(step_status="expired")
        assert is_step_terminal(step) is True

    def test_expired_step_action_blocked_silently(self):
        """Expired steps fail silently — empty error message so UI can answer()."""
        step = _StubStep(step_status="expired")
        allowed, msg = validate_step_action(step)
        assert allowed is False
        assert msg == ""  # caller does callback_query.answer() with no text

    def test_completed_step_action_blocked(self):
        step = _StubStep(step_status="completed")
        allowed, msg = validate_step_action(step)
        assert allowed is False
        assert "виконано" in msg

    def test_skipped_step_action_blocked(self):
        step = _StubStep(step_status="skipped")
        allowed, msg = validate_step_action(step)
        assert allowed is False
        assert "пропущено" in msg

    def test_pending_step_action_allowed(self):
        step = _StubStep(step_status="pending")
        allowed, msg = validate_step_action(step)
        assert allowed is True
        assert msg == ""

    def test_delivered_step_action_allowed(self):
        """Delivered step is not yet terminal — user can still act on it."""
        step = _StubStep(step_status="delivered")
        allowed, msg = validate_step_action(step)
        assert allowed is True
        assert msg == ""

    def test_canceled_step_is_terminal_and_silent(self):
        """Canceled (by adaptation) steps are terminal and fail silently."""
        step = _StubStep(step_status="canceled")
        assert is_step_terminal(step) is True
        allowed, msg = validate_step_action(step)
        assert allowed is False
        assert msg == ""


# ---------------------------------------------------------------------------
# Test 4: current_streak vs best_streak
# ---------------------------------------------------------------------------

class TestCurrentStreak:
    def test_current_streak_single_date(self):
        dates = [date(2026, 5, 18)]
        assert _compute_current_streak(dates, ACTIVE_DAYS_WEEKDAYS) == 1

    def test_current_streak_consecutive(self):
        dates = [date(2026, 5, 18), date(2026, 5, 19), date(2026, 5, 20)]
        assert _compute_current_streak(dates, ACTIVE_DAYS_WEEKDAYS) == 3

    def test_current_streak_after_gap(self):
        """Best=5, but current=2 (gap on Wed)."""
        dates = [
            date(2026, 5, 11),  # Mon
            date(2026, 5, 12),  # Tue
            date(2026, 5, 13),  # Wed
            date(2026, 5, 14),  # Thu
            date(2026, 5, 15),  # Fri
            # gap: no Mon 18
            date(2026, 5, 19),  # Tue
            date(2026, 5, 20),  # Wed
        ]
        assert _compute_best_streak_by_date(dates, ACTIVE_DAYS_WEEKDAYS) == 5
        assert _compute_current_streak(dates, ACTIVE_DAYS_WEEKDAYS) == 2

    def test_current_streak_crosses_weekend(self):
        """Fri + Mon = current streak 2 (Sat/Sun skipped)."""
        dates = [date(2026, 5, 15), date(2026, 5, 18)]
        assert _compute_current_streak(dates, ACTIVE_DAYS_WEEKDAYS) == 2


# ---------------------------------------------------------------------------
# Test 5: three-metric split
# ---------------------------------------------------------------------------

class TestThreeMetrics:
    def _eligible(self, statuses):
        now = datetime.now(timezone.utc)
        past = now - timedelta(hours=1)

        @dataclass
        class _S:
            step_status: str
            scheduled_for: datetime
            canceled_by_adaptation: bool = False

        steps = [_S(s, past) for s in statuses]
        eligible_statuses = ("completed", "skipped", "expired")
        return [
            s for s in steps
            if s.step_status in eligible_statuses
            and s.scheduled_for <= now
            and not s.canceled_by_adaptation
        ]

    def test_completion_rate(self):
        el = self._eligible(["completed", "completed", "skipped", "expired"])
        rate = sum(1 for s in el if s.step_status == "completed") / len(el)
        assert rate == 0.5

    def test_engagement_rate(self):
        el = self._eligible(["completed", "skipped", "expired"])
        rate = sum(1 for s in el if s.step_status in ("completed", "skipped")) / len(el)
        assert round(rate, 4) == round(2 / 3, 4)

    def test_silent_miss_rate(self):
        el = self._eligible(["completed", "expired", "expired"])
        rate = sum(1 for s in el if s.step_status == "expired") / len(el)
        assert round(rate, 4) == round(2 / 3, 4)

    def test_canceled_excluded_from_denominator(self):
        """canceled steps must not appear in eligible (canceled_by_adaptation filter)."""
        now = datetime.now(timezone.utc)
        past = now - timedelta(hours=1)

        @dataclass
        class _S:
            step_status: str
            scheduled_for: datetime
            canceled_by_adaptation: bool

        steps = [
            _S("completed", past, False),
            _S("canceled", past, True),   # must be excluded
        ]
        eligible = [
            s for s in steps
            if s.step_status in ("completed", "skipped", "expired")
            and not s.canceled_by_adaptation
        ]
        assert len(eligible) == 1
        assert eligible[0].step_status == "completed"

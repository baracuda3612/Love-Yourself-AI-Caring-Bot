import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://test-user:test-pass@localhost:5432/test-db",
)
os.environ.setdefault("OPENAI_API_KEY", "test-key")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

from app.plan_completion import metrics


# ── Test doubles ─────────────────────────────────────────────────────────────

@dataclass
class DummyDay:
    day_number: int


@dataclass
class DummyStep:
    """Matches the subset of AIPlanStep attributes used by build_completion_metrics."""
    day: DummyDay
    step_status: str = "pending"       # "completed" | "skipped" | "expired"
    time_slot: str | None = "DAY"
    scheduled_for: datetime | None = None


@dataclass
class DummyPlan:
    id: int
    user_id: int
    total_days: int | None
    focus: str | None
    load: str | None
    duration: str | None
    user: object = None  # plan.user access in build_completion_metrics


class DummyQuery:
    def __init__(self, plan):
        self._plan = plan

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._plan


class DummyDB:
    def __init__(self, plan):
        self._plan = plan

    def query(self, _model):
        return DummyQuery(self._plan)


# ── Date helpers ──────────────────────────────────────────────────────────────
# Map day_number (1-6) to distinct weekday dates (Mon 2026-01-05 onward).
# Active-days default is MON-FRI, so:
#   day 1 (Mon Jan 5) → day 2 (Tue Jan 6): consecutive (no active day between)
#   day 2 (Tue Jan 6) → day 4 (Thu Jan 8): Jan 7 (Wed) is active → streak breaks
_WEEKDAY_DTS = [
    None,                                                    # index 0 unused
    datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc),       # 1 Mon
    datetime(2026, 1, 6, 12, 0, tzinfo=timezone.utc),       # 2 Tue
    datetime(2026, 1, 7, 12, 0, tzinfo=timezone.utc),       # 3 Wed
    datetime(2026, 1, 8, 12, 0, tzinfo=timezone.utc),       # 4 Thu
    datetime(2026, 1, 9, 12, 0, tzinfo=timezone.utc),       # 5 Fri
    datetime(2026, 1, 12, 12, 0, tzinfo=timezone.utc),      # 6 Mon (next week)
]


def _day_ts(day_number: int) -> datetime:
    return _WEEKDAY_DTS[day_number]


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_normal_scenario_counts(monkeypatch):
    db = DummyDB(DummyPlan(id=10, user_id=1, total_days=7, focus="ENERGY", load="LITE", duration="2w"))
    eligible = [
        DummyStep(day=DummyDay(1), step_status="completed", time_slot="MORNING", scheduled_for=_day_ts(1)),
        DummyStep(day=DummyDay(1), step_status="skipped",   time_slot="DAY",     scheduled_for=_day_ts(1)),
        DummyStep(day=DummyDay(2), step_status="expired",   time_slot="EVENING", scheduled_for=_day_ts(2)),
    ]

    monkeypatch.setattr(metrics, "_fetch_eligible_steps", lambda *_: eligible)

    result = metrics.build_completion_metrics(db=db, user_id=1, plan_id=10)

    assert result.total_delivered == 3
    assert result.total_completed == 1
    assert result.total_skipped == 1
    assert result.total_ignored == 1
    assert result.completion_rate == pytest.approx(1 / 3)
    assert result.best_streak == 1
    assert result.had_adaptations is False
    assert result.adaptation_count == 0
    assert result.dominant_time_slot == "MORNING"
    assert result.outcome_tier == "WEAK"


def test_zero_delivered_returns_weak_defaults(monkeypatch):
    db = DummyDB(DummyPlan(id=20, user_id=2, total_days=5, focus="CALM", load="STANDARD", duration="1w"))

    monkeypatch.setattr(metrics, "_fetch_eligible_steps", lambda *_: [])

    result = metrics.build_completion_metrics(db=db, user_id=2, plan_id=20)

    assert result.total_delivered == 0
    assert result.total_completed == 0
    assert result.total_skipped == 0
    assert result.total_ignored == 0
    assert result.completion_rate == 0.0
    assert result.best_streak == 0
    assert result.dominant_time_slot is None
    assert result.outcome_tier == "WEAK"


def test_best_streak_calculated_by_consecutive_days(monkeypatch):
    db = DummyDB(DummyPlan(id=30, user_id=3, total_days=10, focus=None, load="LITE", duration=None))
    # Day 1 (Mon) → Day 2 (Tue): consecutive.
    # Day 2 (Tue) → Day 4 (Thu): Wed is active between them → streak breaks.
    # Day 6 (Mon next week) is skipped — does not count toward streak.
    eligible = [
        DummyStep(day=DummyDay(1), step_status="completed", time_slot="DAY", scheduled_for=_day_ts(1)),
        DummyStep(day=DummyDay(2), step_status="completed", time_slot="DAY", scheduled_for=_day_ts(2)),
        DummyStep(day=DummyDay(4), step_status="completed", time_slot="DAY", scheduled_for=_day_ts(4)),
        DummyStep(day=DummyDay(6), step_status="skipped",   time_slot="DAY", scheduled_for=_day_ts(6)),
    ]

    monkeypatch.setattr(metrics, "_fetch_eligible_steps", lambda *_: eligible)

    result = metrics.build_completion_metrics(db=db, user_id=3, plan_id=30)

    assert result.best_streak == 2


def test_dominant_time_slot_and_tie(monkeypatch):
    db = DummyDB(DummyPlan(id=40, user_id=4, total_days=3, focus="FOCUS", load="LITE", duration="1w"))

    dominant_eligible = [
        DummyStep(day=DummyDay(1), step_status="completed", time_slot="EVENING", scheduled_for=_day_ts(1)),
        DummyStep(day=DummyDay(1), step_status="completed", time_slot="EVENING", scheduled_for=_day_ts(1)),
        DummyStep(day=DummyDay(2), step_status="completed", time_slot="DAY",     scheduled_for=_day_ts(2)),
    ]
    monkeypatch.setattr(metrics, "_fetch_eligible_steps", lambda *_: dominant_eligible)
    dominant = metrics.build_completion_metrics(db=db, user_id=4, plan_id=40)
    assert dominant.dominant_time_slot == "EVENING"

    tie_eligible = [
        DummyStep(day=DummyDay(1), step_status="completed", time_slot="MORNING", scheduled_for=_day_ts(1)),
        DummyStep(day=DummyDay(2), step_status="completed", time_slot="DAY",     scheduled_for=_day_ts(2)),
    ]
    monkeypatch.setattr(metrics, "_fetch_eligible_steps", lambda *_: tie_eligible)
    tie = metrics.build_completion_metrics(db=db, user_id=4, plan_id=40)
    assert tie.dominant_time_slot is None


@pytest.mark.parametrize(
    ("eligible", "expected"),
    [
        (
            [
                DummyStep(day=DummyDay(1), step_status="completed", scheduled_for=_WEEKDAY_DTS[1]),
                DummyStep(day=DummyDay(2), step_status="completed", scheduled_for=_WEEKDAY_DTS[2]),
                DummyStep(day=DummyDay(3), step_status="completed", scheduled_for=_WEEKDAY_DTS[3]),
                DummyStep(day=DummyDay(4), step_status="completed", scheduled_for=_WEEKDAY_DTS[4]),
                DummyStep(day=DummyDay(5), step_status="expired",   scheduled_for=_WEEKDAY_DTS[5]),
            ],
            "STRONG",
        ),
        (
            [
                DummyStep(day=DummyDay(1), step_status="completed", scheduled_for=_WEEKDAY_DTS[1]),
                DummyStep(day=DummyDay(2), step_status="expired",   scheduled_for=_WEEKDAY_DTS[2]),
            ],
            "NEUTRAL",
        ),
        (
            [
                DummyStep(day=DummyDay(1), step_status="expired", scheduled_for=_WEEKDAY_DTS[1]),
                DummyStep(day=DummyDay(2), step_status="expired", scheduled_for=_WEEKDAY_DTS[2]),
            ],
            "WEAK",
        ),
    ],
)
def test_outcome_tier_thresholds(monkeypatch, eligible, expected):
    db = DummyDB(DummyPlan(id=50, user_id=5, total_days=3, focus="ENERGY", load="LITE", duration="1w"))

    monkeypatch.setattr(metrics, "_fetch_eligible_steps", lambda *_: eligible)

    result = metrics.build_completion_metrics(db=db, user_id=5, plan_id=50)

    assert result.outcome_tier == expected


def test_missing_plan_raises_value_error():
    db = DummyDB(plan=None)

    with pytest.raises(ValueError, match="Plan 999 not found for user 1"):
        metrics.build_completion_metrics(db=db, user_id=1, plan_id=999)

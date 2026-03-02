import os
import sys
from dataclasses import dataclass
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


@dataclass
class DummyDay:
    day_number: int


@dataclass
class DummyStep:
    day: DummyDay
    is_completed: bool = False
    skipped: bool = False
    time_slot: str | None = "DAY"


@dataclass
class DummyPlan:
    id: int
    user_id: int
    total_days: int | None
    focus: str | None
    load: str | None
    duration: str | None


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


def _ts(value: int) -> datetime:
    return datetime.fromtimestamp(value, tz=timezone.utc)


def test_normal_scenario_counts(monkeypatch):
    db = DummyDB(DummyPlan(id=10, user_id=1, total_days=7, focus="ENERGY", load="LITE", duration="2w"))
    delivered = [
        (DummyStep(day=DummyDay(1), is_completed=True, time_slot="MORNING"), _ts(1)),
        (DummyStep(day=DummyDay(1), skipped=True, time_slot="DAY"), _ts(2)),
        (DummyStep(day=DummyDay(2), is_completed=False, skipped=False, time_slot="EVENING"), _ts(3)),
    ]

    monkeypatch.setattr(metrics, "fetch_delivered_steps", lambda *_args, **_kwargs: delivered)
    monkeypatch.setattr(metrics, "get_adaptation_count", lambda *_args, **_kwargs: 2)

    result = metrics.build_completion_metrics(db=db, user_id=1, plan_id=10)

    assert result.total_delivered == 3
    assert result.total_completed == 1
    assert result.total_skipped == 1
    assert result.total_ignored == 1
    assert result.completion_rate == pytest.approx(1 / 3)
    assert result.best_streak == 1
    assert result.had_adaptations is True
    assert result.adaptation_count == 2
    assert result.dominant_time_slot == "MORNING"
    assert result.outcome_tier == "WEAK"


def test_zero_delivered_returns_weak_defaults(monkeypatch):
    db = DummyDB(DummyPlan(id=20, user_id=2, total_days=5, focus="CALM", load="STANDARD", duration="1w"))

    monkeypatch.setattr(metrics, "fetch_delivered_steps", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(metrics, "get_adaptation_count", lambda *_args, **_kwargs: 0)

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
    delivered = [
        (DummyStep(day=DummyDay(1), is_completed=True, time_slot="DAY"), _ts(1)),
        (DummyStep(day=DummyDay(2), is_completed=True, time_slot="DAY"), _ts(2)),
        (DummyStep(day=DummyDay(4), is_completed=True, time_slot="DAY"), _ts(3)),
        (DummyStep(day=DummyDay(6), is_completed=False, skipped=True, time_slot="DAY"), _ts(4)),
    ]

    monkeypatch.setattr(metrics, "fetch_delivered_steps", lambda *_args, **_kwargs: delivered)
    monkeypatch.setattr(metrics, "get_adaptation_count", lambda *_args, **_kwargs: 1)

    result = metrics.build_completion_metrics(db=db, user_id=3, plan_id=30)

    assert result.best_streak == 2


def test_dominant_time_slot_and_tie(monkeypatch):
    db = DummyDB(DummyPlan(id=40, user_id=4, total_days=3, focus="FOCUS", load="LITE", duration="1w"))
    dominant_delivered = [
        (DummyStep(day=DummyDay(1), is_completed=True, time_slot="EVENING"), _ts(1)),
        (DummyStep(day=DummyDay(1), is_completed=True, time_slot="EVENING"), _ts(2)),
        (DummyStep(day=DummyDay(2), is_completed=True, time_slot="DAY"), _ts(3)),
    ]

    monkeypatch.setattr(metrics, "fetch_delivered_steps", lambda *_args, **_kwargs: dominant_delivered)
    monkeypatch.setattr(metrics, "get_adaptation_count", lambda *_args, **_kwargs: 0)
    dominant = metrics.build_completion_metrics(db=db, user_id=4, plan_id=40)
    assert dominant.dominant_time_slot == "EVENING"

    tie_delivered = [
        (DummyStep(day=DummyDay(1), is_completed=True, time_slot="MORNING"), _ts(1)),
        (DummyStep(day=DummyDay(2), is_completed=True, time_slot="DAY"), _ts(2)),
    ]
    monkeypatch.setattr(metrics, "fetch_delivered_steps", lambda *_args, **_kwargs: tie_delivered)
    tie = metrics.build_completion_metrics(db=db, user_id=4, plan_id=40)
    assert tie.dominant_time_slot is None


@pytest.mark.parametrize(
    ("delivered", "expected"),
    [
        (
            [
                (DummyStep(day=DummyDay(1), is_completed=True), _ts(1)),
                (DummyStep(day=DummyDay(2), is_completed=True), _ts(2)),
                (DummyStep(day=DummyDay(3), is_completed=True), _ts(3)),
                (DummyStep(day=DummyDay(4), is_completed=True), _ts(4)),
                (DummyStep(day=DummyDay(5), is_completed=False), _ts(5)),
            ],
            "STRONG",
        ),
        (
            [
                (DummyStep(day=DummyDay(1), is_completed=True), _ts(1)),
                (DummyStep(day=DummyDay(2), is_completed=False), _ts(2)),
            ],
            "NEUTRAL",
        ),
        (
            [
                (DummyStep(day=DummyDay(1), is_completed=False), _ts(1)),
                (DummyStep(day=DummyDay(2), is_completed=False), _ts(2)),
            ],
            "WEAK",
        ),
    ],
)
def test_outcome_tier_thresholds(monkeypatch, delivered, expected):
    db = DummyDB(DummyPlan(id=50, user_id=5, total_days=3, focus="ENERGY", load="LITE", duration="1w"))

    monkeypatch.setattr(metrics, "fetch_delivered_steps", lambda *_args, **_kwargs: delivered)
    monkeypatch.setattr(metrics, "get_adaptation_count", lambda *_args, **_kwargs: 0)

    result = metrics.build_completion_metrics(db=db, user_id=5, plan_id=50)

    assert result.outcome_tier == expected


def test_missing_plan_raises_value_error():
    db = DummyDB(plan=None)

    with pytest.raises(ValueError, match="Plan 999 not found for user 1"):
        metrics.build_completion_metrics(db=db, user_id=1, plan_id=999)

import os
import sys
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timezone

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

from app import plan_metrics


@dataclass
class DummyStep:
    is_completed: bool = False
    skipped: bool = False


def _ts(value: int) -> datetime:
    return datetime.fromtimestamp(value, tz=timezone.utc)


def test_skip_streak_no_delivered_tasks(monkeypatch):
    monkeypatch.setattr(plan_metrics, "_fetch_delivered_steps", lambda *_args, **_kwargs: [])

    assert plan_metrics.calculate_skip_streak(db=None, user_id=1, plan_id=1) == 0


def test_skip_streak_consecutive_skips(monkeypatch):
    delivered = [
        (DummyStep(skipped=True), _ts(3)),
        (DummyStep(skipped=True), _ts(2)),
        (DummyStep(skipped=True), _ts(1)),
    ]
    monkeypatch.setattr(plan_metrics, "_fetch_delivered_steps", lambda *_args, **_kwargs: delivered)
    monkeypatch.setattr(plan_metrics, "_fetch_reset_events", lambda *_args, **_kwargs: [])

    assert plan_metrics.calculate_skip_streak(db=None, user_id=1, plan_id=1) == 3


def test_skip_streak_completed_task_stops(monkeypatch):
    delivered = [
        (DummyStep(is_completed=True), _ts(3)),
        (DummyStep(skipped=True), _ts(2)),
    ]
    monkeypatch.setattr(plan_metrics, "_fetch_delivered_steps", lambda *_args, **_kwargs: delivered)
    monkeypatch.setattr(plan_metrics, "_fetch_reset_events", lambda *_args, **_kwargs: [])

    assert plan_metrics.calculate_skip_streak(db=None, user_id=1, plan_id=1) == 0


def test_skip_streak_in_progress_stops(monkeypatch):
    delivered = [
        (DummyStep(is_completed=False, skipped=False), _ts(3)),
        (DummyStep(skipped=True), _ts(2)),
    ]
    monkeypatch.setattr(plan_metrics, "_fetch_delivered_steps", lambda *_args, **_kwargs: delivered)
    monkeypatch.setattr(plan_metrics, "_fetch_reset_events", lambda *_args, **_kwargs: [])

    assert plan_metrics.calculate_skip_streak(db=None, user_id=1, plan_id=1) == 1


def test_skip_streak_reset_event_stops(monkeypatch):
    delivered = [
        (DummyStep(skipped=True), _ts(3)),
        (DummyStep(skipped=True), _ts(2)),
    ]
    resets = [_ts(2.5)]
    monkeypatch.setattr(plan_metrics, "_fetch_delivered_steps", lambda *_args, **_kwargs: delivered)
    monkeypatch.setattr(plan_metrics, "_fetch_reset_events", lambda *_args, **_kwargs: resets)

    assert plan_metrics.calculate_skip_streak(db=None, user_id=1, plan_id=1) == 1


def test_skip_streak_scheduler_failure(monkeypatch):
    monkeypatch.setattr(plan_metrics, "_fetch_delivered_steps", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(plan_metrics, "_fetch_reset_events", lambda *_args, **_kwargs: [])

    assert plan_metrics.calculate_skip_streak(db=None, user_id=1, plan_id=1) == 0


@pytest.mark.parametrize(
    ("delivered", "expected"),
    [
        ([], 0.0),
        ([(DummyStep(is_completed=True), _ts(1))], 1.0),
        (
            [
                (DummyStep(is_completed=True), _ts(3)),
                (DummyStep(is_completed=False), _ts(2)),
                (DummyStep(is_completed=False), _ts(1)),
            ],
            1 / 3,
        ),
    ],
)
def test_completion_rate(monkeypatch, delivered, expected):
    monkeypatch.setattr(plan_metrics, "_fetch_delivered_steps", lambda *_args, **_kwargs: delivered)

    assert plan_metrics.get_completion_rate(db=None, user_id=1, plan_id=1) == expected


def test_get_recent_tasks(monkeypatch):
    delivered = [
        (DummyStep(skipped=True), _ts(3)),
        (DummyStep(skipped=False), _ts(2)),
        (DummyStep(skipped=False), _ts(1)),
    ]
    monkeypatch.setattr(plan_metrics, "_fetch_delivered_steps", lambda *_args, **_kwargs: delivered)

    result = plan_metrics.get_recent_tasks(db=None, user_id=1, plan_id=1, limit=2)

    assert result == [delivered[0][0], delivered[1][0]]

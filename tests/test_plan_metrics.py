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
    step_status: str = "pending"


def _ts(value: int) -> datetime:
    return datetime.fromtimestamp(value, tz=timezone.utc)


def test_skip_streak_no_delivered_tasks(monkeypatch):
    monkeypatch.setattr(plan_metrics, "fetch_delivered_steps", lambda *_args, **_kwargs: [])

    assert plan_metrics.calculate_skip_streak(db=None, user_id=1, plan_id=1) == 0


def test_skip_streak_consecutive_skips(monkeypatch):
    delivered = [
        (DummyStep("skipped"), _ts(3)),
        (DummyStep("skipped"), _ts(2)),
        (DummyStep("skipped"), _ts(1)),
    ]
    monkeypatch.setattr(plan_metrics, "fetch_delivered_steps", lambda *_args, **_kwargs: delivered)
    monkeypatch.setattr(plan_metrics, "_fetch_reset_events", lambda *_args, **_kwargs: [])

    assert plan_metrics.calculate_skip_streak(db=None, user_id=1, plan_id=1) == 3


def test_skip_streak_completed_task_stops(monkeypatch):
    delivered = [
        (DummyStep("completed"), _ts(3)),
        (DummyStep("skipped"), _ts(2)),
    ]
    monkeypatch.setattr(plan_metrics, "fetch_delivered_steps", lambda *_args, **_kwargs: delivered)
    monkeypatch.setattr(plan_metrics, "_fetch_reset_events", lambda *_args, **_kwargs: [])

    assert plan_metrics.calculate_skip_streak(db=None, user_id=1, plan_id=1) == 0


def test_skip_streak_in_progress_stops(monkeypatch):
    delivered = [
        (DummyStep("pending"), _ts(3)),
        (DummyStep("skipped"), _ts(2)),
    ]
    monkeypatch.setattr(plan_metrics, "fetch_delivered_steps", lambda *_args, **_kwargs: delivered)
    monkeypatch.setattr(plan_metrics, "_fetch_reset_events", lambda *_args, **_kwargs: [])

    assert plan_metrics.calculate_skip_streak(db=None, user_id=1, plan_id=1) == 1


def test_skip_streak_reset_event_stops(monkeypatch):
    delivered = [
        (DummyStep("skipped"), _ts(3)),
        (DummyStep("skipped"), _ts(2)),
    ]
    resets = [_ts(2.5)]
    monkeypatch.setattr(plan_metrics, "fetch_delivered_steps", lambda *_args, **_kwargs: delivered)
    monkeypatch.setattr(plan_metrics, "_fetch_reset_events", lambda *_args, **_kwargs: resets)

    assert plan_metrics.calculate_skip_streak(db=None, user_id=1, plan_id=1) == 1


def test_skip_streak_scheduler_failure(monkeypatch):
    monkeypatch.setattr(plan_metrics, "fetch_delivered_steps", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(plan_metrics, "_fetch_reset_events", lambda *_args, **_kwargs: [])

    assert plan_metrics.calculate_skip_streak(db=None, user_id=1, plan_id=1) == 0


@pytest.mark.parametrize("statuses,expected", [([],0.0),(['completed'],1.0),(['completed','skipped','expired'],1/3)])
def test_completion_rate_uses_scheduled_terminal_denominator(statuses,expected):
    clauses=[]
    class Query:
        def join(self,*args): return self
        def filter(self,*args):
            clauses.extend(str(a.compile(compile_kwargs={"literal_binds":True})) for a in args)
            return self
        def all(self): return [DummyStep(s) for s in statuses]
    class DB:
        def query(self,*args): return Query()
    assert plan_metrics.get_completion_rate(DB(),1,1)==expected
    assert any("scheduled_for <=" in c for c in clauses)
    assert any("'completed', 'skipped', 'expired'" in c for c in clauses)
    assert not any('user_events' in c for c in clauses)


def test_get_recent_tasks(monkeypatch):
    delivered = [
        (DummyStep("skipped"), _ts(3)),
        (DummyStep("pending"), _ts(2)),
        (DummyStep("pending"), _ts(1)),
    ]
    monkeypatch.setattr(plan_metrics, "fetch_delivered_steps", lambda *_args, **_kwargs: delivered)

    result = plan_metrics.get_recent_tasks(db=None, user_id=1, plan_id=1, limit=2)

    assert result == [delivered[0][0], delivered[1][0]]

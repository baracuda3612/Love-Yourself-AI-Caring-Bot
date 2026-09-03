import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://test-user:test-pass@localhost:5432/test-db",
)
os.environ.setdefault("OPENAI_API_KEY", "test-key")

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

from app import time_slots


class _PlanQuery:
    def __init__(self, plans):
        self.plans = plans
        self.filters = []

    def options(self, *_args):
        return self

    def filter(self, *criteria):
        self.filters.extend(criteria)
        return self

    def all(self):
        return self.plans


class _DB:
    def __init__(self, plans):
        self.query_result = _PlanQuery(plans)
        self.added = []

    def query(self, model):
        assert model is time_slots.AIPlan
        return self.query_result

    def add(self, value):
        self.added.append(value)


def test_time_change_recomputes_active_and_paused_plans(monkeypatch):
    plans = [
        SimpleNamespace(id=10, status="active"),
        SimpleNamespace(id=20, status="paused"),
    ]
    db = _DB(plans)
    user = SimpleNamespace(
        id=1,
        profile=SimpleNamespace(daily_time_slots={"DAY": "14:00"}),
    )
    captured = {}

    def _fake_recompute(candidate_user, candidate_plans, normalized):
        captured.update(
            user=candidate_user,
            plans=candidate_plans,
            normalized=normalized,
        )
        return [101, 202], [101]

    monkeypatch.setattr(time_slots, "recompute_future_steps", _fake_recompute)

    result = time_slots.update_user_time_slots(db, user, {"DAY": "15:30"})

    assert result == ([101, 202], [101])
    assert captured["plans"] == plans
    assert captured["normalized"]["DAY"] == "15:30"
    assert any(
        getattr(getattr(criterion, "right", None), "value", None)
        == ["active", "paused"]
        for criterion in db.query_result.filters
    )


def test_paused_plan_steps_are_updated_without_rescheduling_jobs():
    effective_from = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)
    original_schedule = effective_from + timedelta(days=1)
    step = SimpleNamespace(
        id=202,
        step_status="pending",
        time_slot="DAY",
        scheduled_for=original_schedule,
    )
    day = SimpleNamespace(day_number=1, steps=[step])
    plan = SimpleNamespace(
        status="paused",
        start_date=effective_from,
        days=[day],
    )
    user = SimpleNamespace(timezone="UTC")

    updated_ids, active_ids = time_slots.recompute_future_steps(
        user,
        [plan],
        {"MORNING": "09:30", "DAY": "15:30", "EVENING": "21:00"},
        effective_from=effective_from,
    )

    assert updated_ids == [202]
    assert active_ids == []
    assert step.scheduled_for.hour == 15
    assert step.scheduled_for.minute == 30

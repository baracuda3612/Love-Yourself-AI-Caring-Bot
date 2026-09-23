import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

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


def test_resume_reanchors_remaining_days_without_replaying_delivered_work():
    resumed_at = datetime(2026, 9, 4, 18, tzinfo=timezone.utc)  # Friday
    delivered_at = datetime(2026, 9, 3, 14, tzinfo=timezone.utc)
    delivered = SimpleNamespace(
        id=20,
        step_status="delivered",
        order_in_day=0,
        time_slot="DAY",
        scheduled_for=delivered_at,
        expires_at=delivered_at + timedelta(hours=10),
    )
    day_three_day = SimpleNamespace(
        id=31,
        step_status="pending",
        order_in_day=0,
        time_slot="DAY",
        scheduled_for=datetime(2026, 9, 4, 14, tzinfo=timezone.utc),
        expires_at=None,
    )
    day_three_evening = SimpleNamespace(
        id=32,
        step_status="pending",
        order_in_day=1,
        time_slot="EVENING",
        scheduled_for=datetime(2026, 9, 4, 20, tzinfo=timezone.utc),
        expires_at=None,
    )
    day_four = SimpleNamespace(
        id=41,
        step_status="pending",
        order_in_day=0,
        time_slot="DAY",
        scheduled_for=datetime(2026, 9, 5, 14, tzinfo=timezone.utc),
        expires_at=None,
    )
    plan = SimpleNamespace(
        start_date=datetime(2026, 9, 1, tzinfo=timezone.utc),
        days=[
            SimpleNamespace(day_number=2, steps=[delivered]),
            SimpleNamespace(day_number=3, steps=[day_three_day, day_three_evening]),
            SimpleNamespace(day_number=4, steps=[day_four]),
        ],
    )
    user = SimpleNamespace(
        timezone="UTC",
        profile=SimpleNamespace(
            active_days=["MON", "TUE", "WED", "THU", "FRI"],
            daily_time_slots={"DAY": "15:30", "EVENING": "20:30"},
        ),
    )

    updated = time_slots.reanchor_pending_plan_steps(
        user, plan, resumed_at=resumed_at
    )

    assert updated == [31, 32, 41]
    assert delivered.scheduled_for == delivered_at
    assert day_three_day.scheduled_for == datetime(2026, 9, 7, 15, 30, tzinfo=timezone.utc)
    assert day_three_evening.scheduled_for == datetime(2026, 9, 7, 20, 30, tzinfo=timezone.utc)
    assert day_four.scheduled_for == datetime(2026, 9, 8, 15, 30, tzinfo=timezone.utc)
    assert day_three_day.expires_at == datetime(2026, 9, 7, 23, 59, 59, tzinfo=timezone.utc)


def test_resume_invalid_slot_time_leaves_every_pending_step_unchanged():
    original = datetime(2026, 9, 4, 14, tzinfo=timezone.utc)
    first = SimpleNamespace(
        id=31, step_status="pending", order_in_day=0,
        time_slot="DAY", scheduled_for=original, expires_at=None,
    )
    second = SimpleNamespace(
        id=32, step_status="pending", order_in_day=1,
        time_slot="EVENING", scheduled_for=original, expires_at=None,
    )
    plan = SimpleNamespace(
        start_date=original,
        days=[SimpleNamespace(day_number=3, steps=[first, second])],
    )
    user = SimpleNamespace(
        timezone="UTC",
        profile=SimpleNamespace(
            active_days=["MON", "TUE", "WED", "THU", "FRI"],
            daily_time_slots={"DAY": "15:30", "EVENING": "24:00"},
        ),
    )

    with pytest.raises(time_slots.TimeSlotError, match="invalid_time_range"):
        time_slots.reanchor_pending_plan_steps(user, plan, resumed_at=original)

    assert first.scheduled_for == original
    assert second.scheduled_for == original

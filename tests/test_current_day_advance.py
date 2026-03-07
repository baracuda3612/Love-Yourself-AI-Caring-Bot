from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.ux.task_notification import maybe_advance_current_day


@dataclass
class _Step:
    scheduled_for: datetime | None
    canceled_by_adaptation: bool = False
    is_delivered: bool = False
    delivered_at: datetime | None = None


@dataclass
class _Day:
    plan_id: int
    day_number: int
    steps: list[_Step]


class _Query:
    def __init__(self, value):
        self.value = value
        self.locked = False

    def filter(self, *_args, **_kwargs):
        return self

    def with_for_update(self):
        self.locked = True
        return self

    def first(self):
        return self.value


class _DB:
    def __init__(self, plan, day):
        self.plan = plan
        self.day = day
        self.plan_query = _Query(plan)
        self.day_query = _Query(day)
        self.added = []

    def query(self, model):
        name = getattr(model, "__name__", "")
        if name == "AIPlan":
            return self.plan_query
        if name == "AIPlanDay":
            return self.day_query
        raise AssertionError(name)

    def add(self, obj):
        self.added.append(obj)


def _dt(minutes_ago: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)


def test_advances_after_last_step_delivered():
    plan = SimpleNamespace(id=1, current_day=3, total_days=21)
    day = _Day(1, 3, [_Step(_dt(10), is_delivered=True), _Step(_dt(5), delivered_at=_dt(1))])
    db = _DB(plan, day)

    advanced = maybe_advance_current_day(db, 1, 3)

    assert advanced is True
    assert plan.current_day == 4


def test_no_advance_if_steps_remaining():
    plan = SimpleNamespace(id=1, current_day=3, total_days=21)
    day = _Day(1, 3, [_Step(_dt(10), is_delivered=True), _Step(_dt(-10))])
    db = _DB(plan, day)

    advanced = maybe_advance_current_day(db, 1, 3)

    assert advanced is False
    assert plan.current_day == 3


def test_canceled_steps_not_counted():
    plan = SimpleNamespace(id=1, current_day=3, total_days=21)
    day = _Day(1, 3, [_Step(_dt(10), is_delivered=True), _Step(_dt(-10), canceled_by_adaptation=True)])
    db = _DB(plan, day)

    advanced = maybe_advance_current_day(db, 1, 3)

    assert advanced is True
    assert plan.current_day == 4


def test_idempotent_if_already_advanced():
    plan = SimpleNamespace(id=1, current_day=5, total_days=21)
    day = _Day(1, 3, [_Step(_dt(10), is_delivered=True)])
    db = _DB(plan, day)

    advanced = maybe_advance_current_day(db, 1, 3)

    assert advanced is False
    assert plan.current_day == 5


def test_no_advance_on_last_day():
    plan = SimpleNamespace(id=1, current_day=21, total_days=21)
    day = _Day(1, 21, [_Step(_dt(10), is_delivered=True)])
    db = _DB(plan, day)

    advanced = maybe_advance_current_day(db, 1, 21)

    assert advanced is False
    assert plan.current_day == 21


def test_single_step_day_advances_immediately():
    plan = SimpleNamespace(id=1, current_day=2, total_days=21)
    day = _Day(1, 2, [_Step(_dt(1), delivered_at=_dt(1))])
    db = _DB(plan, day)

    advanced = maybe_advance_current_day(db, 1, 2)

    assert advanced is True
    assert plan.current_day == 3


def test_multi_step_day_waits_for_all():
    plan = SimpleNamespace(id=1, current_day=2, total_days=21)
    day = _Day(1, 2, [_Step(_dt(10), is_delivered=True), _Step(_dt(-2))])
    db = _DB(plan, day)

    advanced = maybe_advance_current_day(db, 1, 2)
    assert advanced is False
    assert plan.current_day == 2

    day.steps[1].is_delivered = True
    advanced = maybe_advance_current_day(db, 1, 2)
    assert advanced is True
    assert plan.current_day == 3


def test_uses_for_update_lock_on_plan_query():
    plan = SimpleNamespace(id=1, current_day=2, total_days=21)
    day = _Day(1, 2, [_Step(_dt(1), is_delivered=True)])
    db = _DB(plan, day)

    maybe_advance_current_day(db, 1, 2)

    assert db.plan_query.locked is True

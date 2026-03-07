from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import api, scheduler
from app.plan_completion.pulse import build_pulse_data
from app.plan_completion.tokens import make_report_token


@dataclass
class _Step:
    canceled_by_adaptation: bool = False
    is_completed: bool = False
    status: str | None = None


@dataclass
class _Day:
    day_number: int
    steps: list[_Step]
    is_completed: bool = False


class _Query:
    def __init__(self, value):
        self.value = value

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def options(self, *_args, **_kwargs):
        return self

    def all(self):
        return self.value

    def first(self):
        return self.value

    def count(self):
        if isinstance(self.value, int):
            return self.value
        return len(self.value)


class _DB:
    def __init__(self, plan, user, days):
        self.plan = plan
        self.user = user
        self.days = days

    def query(self, model):
        name = model.__name__
        if name == "AIPlan":
            return _Query(self.plan)
        if name == "User":
            return _Query(self.user)
        if name == "AIPlanDay":
            return _Query(self.days)
        raise AssertionError(name)


def _make_days(total=21):
    days = []
    for i in range(1, total + 1):
        days.append(_Day(day_number=i, steps=[]))
    return days


def test_active_day_uses_plan_current_day():
    plan = SimpleNamespace(id=1, user_id=10, total_days=21, current_day=9)
    data = build_pulse_data(1, _DB(plan, SimpleNamespace(profile={}), _make_days(21)))
    assert data.active_day_number == 9


def test_active_day_fallback_when_current_day_missing():
    plan = SimpleNamespace(id=1, user_id=10, total_days=21, current_day=None)
    days = _make_days(21)
    days[0].is_completed = True
    days[1].is_completed = True
    data = build_pulse_data(1, _DB(plan, SimpleNamespace(profile={}), days))
    assert data.active_day_number == 3


def test_window_standard_day7():
    plan = SimpleNamespace(id=1, user_id=10, total_days=21, current_day=7)
    data = build_pulse_data(1, _DB(plan, SimpleNamespace(profile={}), _make_days(21)))
    assert data.week_number == 1
    assert [d.day for d in data.days if d.window == "active"] == list(range(1, 8))


def test_window_standard_day14():
    plan = SimpleNamespace(id=1, user_id=10, total_days=21, current_day=14)
    data = build_pulse_data(1, _DB(plan, SimpleNamespace(profile={}), _make_days(21)))
    assert data.week_number == 2
    assert [d.day for d in data.days if d.window == "active"] == list(range(8, 15))


def test_window_long_day28():
    plan = SimpleNamespace(id=1, user_id=10, total_days=90, current_day=28)
    data = build_pulse_data(1, _DB(plan, SimpleNamespace(profile={}), _make_days(90)))
    assert [d.day for d in data.days if d.window == "active"] == list(range(15, 35))


def test_ratio_1step_day():
    days = _make_days(21)
    days[0].steps = [_Step(is_completed=True)]
    data = build_pulse_data(1, _DB(SimpleNamespace(id=1, user_id=10, total_days=21, current_day=1), SimpleNamespace(profile={}), days))
    assert data.days[0].completion_ratio == 1.0


def test_ratio_2step_day():
    days = _make_days(21)
    days[0].steps = [_Step(is_completed=True), _Step(is_completed=False)]
    data = build_pulse_data(1, _DB(SimpleNamespace(id=1, user_id=10, total_days=21, current_day=1), SimpleNamespace(profile={}), days))
    assert data.days[0].completion_ratio == 0.5


def test_ratio_3step_day_partial():
    days = _make_days(21)
    days[0].steps = [_Step(is_completed=True), _Step(is_completed=False), _Step(is_completed=False)]
    data = build_pulse_data(1, _DB(SimpleNamespace(id=1, user_id=10, total_days=21, current_day=1), SimpleNamespace(profile={}), days))
    assert data.days[0].completion_ratio == 0.33


def test_ratio_ignores_canceled():
    days = _make_days(21)
    days[0].steps = [_Step(is_completed=True), _Step(canceled_by_adaptation=True, is_completed=False)]
    data = build_pulse_data(1, _DB(SimpleNamespace(id=1, user_id=10, total_days=21, current_day=1), SimpleNamespace(profile={}), days))
    assert data.days[0].completion_ratio == 1.0


def test_adapted_via_canceled_step():
    days = _make_days(21)
    days[3].steps = [_Step(canceled_by_adaptation=True)]
    plan = SimpleNamespace(id=1, user_id=10, total_days=21, current_day=14)
    data = build_pulse_data(1, _DB(plan, SimpleNamespace(profile={}), days))
    assert data.days[3].adapted is True


def test_adapted_does_not_affect_ratio():
    days = _make_days(21)
    days[3].steps = [
        _Step(canceled_by_adaptation=True),
        _Step(is_completed=True),
    ]
    plan = SimpleNamespace(id=1, user_id=10, total_days=21, current_day=14)
    data = build_pulse_data(1, _DB(plan, SimpleNamespace(profile={}), days))
    assert data.days[3].adapted is True
    assert data.days[3].completion_ratio == 1.0


def test_is_today_by_active_day():
    data = build_pulse_data(1, _DB(SimpleNamespace(id=1, user_id=10, total_days=21, current_day=5), SimpleNamespace(profile={}), _make_days(21)))
    assert sum(1 for d in data.days if d.is_today) == 1
    assert data.days[4].is_today is True


def test_phrase_deterministic():
    plan = SimpleNamespace(id=7, user_id=10, total_days=21, current_day=8)
    db = _DB(plan, SimpleNamespace(profile={"persona": "empath"}), _make_days(21))
    assert build_pulse_data(7, db).phrase == build_pulse_data(7, db).phrase


@pytest.mark.anyio
async def test_no_pulse_short_plan(monkeypatch):
    bot_calls = []

    class _Bot:
        async def send_message(self, *_args, **_kwargs):
            bot_calls.append(1)

    class _DBShort:
        def query(self, model):
            if model is scheduler.AIPlan:
                return _Query([SimpleNamespace(id=1, user_id=10, duration="SHORT", status="active", current_day=7)])
            if model is scheduler.User:
                return _Query(SimpleNamespace(id=10, tg_id=100, timezone="Europe/Kyiv", current_state="ACTIVE"))
            if model is scheduler.UserEvent:
                return _Query(None)
            raise AssertionError(model)

    await scheduler.check_pulse_triggers(_DBShort(), _Bot())
    assert bot_calls == []


def test_pulse_endpoint_returns_html(monkeypatch):
    token = make_report_token(55, "secret")
    monkeypatch.setattr(api.settings, "REPORT_TOKEN_SECRET", "secret")

    class _Ctx:
        def __init__(self, db):
            self.db = db
        def __enter__(self):
            return self.db
        def __exit__(self, *_):
            return False

    class _ApiDB:
        def query(self, model):
            if model.__name__ == "AIPlan":
                return _Query(SimpleNamespace(id=55, user_id=777, status="active", duration="STANDARD", focus="mixed"))
            if model.__name__ == "User":
                return _Query(SimpleNamespace(id=777, profile={"persona": "empath"}))
            if model.__name__ == "AIPlanDay":
                return _Query(_make_days(21))
            raise AssertionError(model)

    monkeypatch.setattr(api, "SessionLocal", lambda: _Ctx(_ApiDB()))
    client = TestClient(api.app)
    resp = client.get(f"/pulse/{token}")
    assert resp.status_code == 200
    assert "week" in resp.text


def test_pulse_endpoint_inactive_plan(monkeypatch):
    token = make_report_token(55, "secret")
    monkeypatch.setattr(api.settings, "REPORT_TOKEN_SECRET", "secret")

    class _Ctx:
        def __init__(self, db):
            self.db = db
        def __enter__(self):
            return self.db
        def __exit__(self, *_):
            return False

    class _ApiDB:
        def query(self, model):
            if model.__name__ == "AIPlan":
                return _Query(SimpleNamespace(id=55, user_id=777, status="completed", duration="STANDARD", focus="mixed"))
            raise AssertionError(model)

    monkeypatch.setattr(api, "SessionLocal", lambda: _Ctx(_ApiDB()))
    client = TestClient(api.app)
    resp = client.get(f"/pulse/{token}")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_idempotency(monkeypatch):
    bot_calls = []

    class _Bot:
        async def send_message(self, *_args, **_kwargs):
            bot_calls.append(1)

    class _DBIdem:
        def query(self, model):
            if model is scheduler.AIPlan:
                return _Query([SimpleNamespace(id=1, user_id=10, duration="MEDIUM", status="active", current_day=7)])
            if model is scheduler.User:
                return _Query(SimpleNamespace(id=10, tg_id=100, timezone="Europe/Kyiv", current_state="ACTIVE"))
            if model is scheduler.UserEvent:
                return _Query(object())
            raise AssertionError(model)

    await scheduler.check_pulse_triggers(_DBIdem(), _Bot())
    assert bot_calls == []


@pytest.mark.anyio
async def test_inactive_plan_skipped(monkeypatch):
    bot_calls = []

    class _Bot:
        async def send_message(self, *_args, **_kwargs):
            bot_calls.append(1)

    class _DBInactive:
        def query(self, model):
            if model is scheduler.AIPlan:
                return _Query([])
            raise AssertionError(model)

    await scheduler.check_pulse_triggers(_DBInactive(), _Bot())
    assert bot_calls == []

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import api
from app.plan_completion.timeline import get_plan_timeline
from app.plan_completion.tokens import make_report_token, verify_report_token


def test_tokens_roundtrip_plan_id_only():
    token = make_report_token(123, "secret")
    assert verify_report_token(token, "secret") == 123


def test_tokens_invalid_returns_none():
    assert verify_report_token("bad-token", "secret") is None


def test_tokens_tampered_returns_none():
    token = make_report_token(123, "secret")
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert verify_report_token(tampered, "secret") is None


def test_tokens_padding_edge_case_no_extra_padding_needed():
    token = make_report_token(1, "secret")
    if len(token) % 4 == 0:
        assert verify_report_token(token, "secret") == 1


@dataclass
class _Step:
    id: int
    canceled_by_adaptation: bool = False
    scheduled_for: datetime | None = None


@dataclass
class _Day:
    day_number: int
    steps: list[_Step]


class _PlanQuery:
    def __init__(self, plan):
        self.plan = plan

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self.plan


class _DaysQuery:
    def __init__(self, days):
        self.days = days

    def filter(self, *_args, **_kwargs):
        return self

    def options(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def all(self):
        return self.days


class _EventsQuery:
    def __init__(self, events):
        self.events = events

    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        return self.events


class _TimelineDB:
    def __init__(self, plan, days, events):
        self.plan = plan
        self.days = days
        self.events = events

    def query(self, model):
        name = model.__name__
        if name == "AIPlan":
            return _PlanQuery(self.plan)
        if name == "AIPlanDay":
            return _DaysQuery(self.days)
        if name == "UserEvent":
            return _EventsQuery(self.events)
        raise AssertionError(name)


def test_timeline_statuses_done_partial_skipped_ignored():
    now = datetime.now(timezone.utc) - timedelta(days=1)
    days = [
        _Day(1, [_Step(1, scheduled_for=now)]),
        _Day(2, [_Step(2, scheduled_for=now), _Step(3, scheduled_for=now)]),
        _Day(3, [_Step(4, scheduled_for=now)]),
        _Day(4, [_Step(5, scheduled_for=now)]),
    ]
    events = [
        SimpleNamespace(event_type="task_completed", step_id="1"),
        SimpleNamespace(event_type="task_completed", step_id="2"),
        SimpleNamespace(event_type="task_skipped", step_id="4"),
    ]
    db = _TimelineDB(plan=SimpleNamespace(id=10, user_id=1), days=days, events=events)

    statuses = [x.status for x in get_plan_timeline(db, user_id=1, plan_id=10)]
    assert statuses == ["done", "partial", "skipped", "ignored"]


def test_timeline_future_days():
    future = datetime.now(timezone.utc) + timedelta(days=1)
    days = [_Day(1, [_Step(1, scheduled_for=future)])]
    db = _TimelineDB(plan=SimpleNamespace(id=10, user_id=1), days=days, events=[])

    timeline = get_plan_timeline(db, user_id=1, plan_id=10)
    assert timeline and timeline[0].status == "future"


def test_timeline_all_canceled_returns_empty():
    days = [_Day(1, [_Step(1, canceled_by_adaptation=True)])]
    db = _TimelineDB(plan=SimpleNamespace(id=10, user_id=1), days=days, events=[])
    assert get_plan_timeline(db, user_id=1, plan_id=10) == []


def test_timeline_empty_plan_step_ids_returns_empty():
    days = [_Day(1, [])]
    db = _TimelineDB(plan=SimpleNamespace(id=10, user_id=1), days=days, events=[])
    assert get_plan_timeline(db, user_id=1, plan_id=10) == []


class _ContextSession:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, exc_type, exc, tb):
        return False


class _ApiQuery:
    def __init__(self, result):
        self.result = result

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self.result


class _ApiDB:
    def __init__(self, plan, user):
        self.plan = plan
        self.user = user

    def query(self, model):
        if model.__name__ == "AIPlan":
            return _ApiQuery(self.plan)
        if model.__name__ == "User":
            return _ApiQuery(self.user)
        raise AssertionError(model)


def test_report_valid_token_200_contains_completion_rate(monkeypatch):
    token = make_report_token(55, "secret")
    monkeypatch.setattr(api.settings, "REPORT_TOKEN_SECRET", "secret")
    monkeypatch.setattr(api, "SessionLocal", lambda: _ContextSession(_ApiDB(SimpleNamespace(id=55, user_id=777), SimpleNamespace(id=777, profile={}))))
    monkeypatch.setattr(api, "build_completion_metrics", lambda *_: SimpleNamespace(completion_rate=0.5, total_days=21, best_streak=3, adaptation_count=1, dominant_time_slot="MORNING", focus="mixed", duration="STANDARD", outcome_tier="NEUTRAL"))
    monkeypatch.setattr(api, "build_completion_report", lambda *_: "header\nbody")
    monkeypatch.setattr(api, "_pick_observation", lambda *_: "obs")
    monkeypatch.setattr(api, "get_next_plan_recommendation", lambda *_: SimpleNamespace(button1_text="A", button2_text="B", button1_params={"duration": "SHORT", "load": "LITE", "focus": "mixed"}, button2_params={"duration": "MEDIUM", "load": "MID", "focus": "rest"}))
    monkeypatch.setattr(api, "get_plan_timeline", lambda *_: [])

    client = TestClient(api.app)
    resp = client.get(f"/report/{token}")
    assert resp.status_code == 200
    assert "50%" in resp.text


def test_report_invalid_token_404(monkeypatch):
    monkeypatch.setattr(api.settings, "REPORT_TOKEN_SECRET", "secret")
    client = TestClient(api.app)
    resp = client.get("/report/invalid")
    assert resp.status_code == 404


def test_report_uses_plan_user_id_not_token_user_id(monkeypatch):
    token = make_report_token(99, "secret")
    monkeypatch.setattr(api.settings, "REPORT_TOKEN_SECRET", "secret")
    captured = {}

    def _metrics(_db, user_id, plan_id):
        captured["user_id"] = user_id
        captured["plan_id"] = plan_id
        return SimpleNamespace(completion_rate=0.5, total_days=21, best_streak=3, adaptation_count=1, dominant_time_slot="MORNING", focus="mixed", duration="STANDARD", outcome_tier="NEUTRAL")

    monkeypatch.setattr(api, "SessionLocal", lambda: _ContextSession(_ApiDB(SimpleNamespace(id=99, user_id=1234), SimpleNamespace(id=1234, profile={}))))
    monkeypatch.setattr(api, "build_completion_metrics", _metrics)
    monkeypatch.setattr(api, "build_completion_report", lambda *_: "header\nbody")
    monkeypatch.setattr(api, "_pick_observation", lambda *_: "obs")
    monkeypatch.setattr(api, "get_next_plan_recommendation", lambda *_: SimpleNamespace(button1_text="A", button2_text="B", button1_params={"duration": "SHORT", "load": "LITE", "focus": "mixed"}, button2_params={"duration": "MEDIUM", "load": "MID", "focus": "rest"}))
    monkeypatch.setattr(api, "get_plan_timeline", lambda *_: [])

    client = TestClient(api.app)
    resp = client.get(f"/report/{token}")
    assert resp.status_code == 200
    assert captured == {"user_id": 1234, "plan_id": 99}


def test_deep_link_format(monkeypatch):
    monkeypatch.setattr(api.settings, "BOT_USERNAME", "mybot")
    url = api._deep_link({"duration": "SHORT", "load": "LITE", "focus": "mixed"})
    assert url == "https://t.me/mybot?start=newplan_SHORT_LITE_mixed"

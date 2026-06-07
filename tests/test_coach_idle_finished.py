import os
import pathlib
import sys
from datetime import datetime, timezone
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

from app.workers import coach_agent


class _DummyQuery:
    def __init__(self, plan):
        self._plan = plan

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._plan


class _DummyDB:
    def __init__(self, plan):
        self._plan = plan

    def query(self, *_args, **_kwargs):
        return _DummyQuery(self._plan)


def test_build_idle_finished_context_returns_dict_for_completed_plan(monkeypatch):
    plan = SimpleNamespace(id=123, user_id=7, status="completed", end_date=datetime.now(timezone.utc))

    def fake_build_metrics(_db, user_id, plan_id):
        assert user_id == 7
        assert plan_id == 123
        return SimpleNamespace(
            total_days=14,
            completion_rate=0.86,
            best_streak=9,
            outcome_tier="STRONG",
        )

    import app.plan_completion.metrics as metrics_mod

    monkeypatch.setattr(metrics_mod, "build_completion_metrics", fake_build_metrics)

    result = coach_agent._build_idle_finished_context(_DummyDB(plan), user_id=7)

    assert result == {
        "total_days": 14,
        "completion_rate": 86,
        "best_streak": 9,
        "outcome_tier": "STRONG",
    }


def test_build_idle_finished_context_no_legacy_fields(monkeypatch):
    """adaptation_count, recommended_* removed in T5.8C — must not appear."""
    plan = SimpleNamespace(id=1, user_id=1, status="completed", end_date=datetime.now(timezone.utc))

    def fake_build_metrics(_db, _uid, _pid):
        return SimpleNamespace(
            total_days=7,
            completion_rate=0.5,
            best_streak=3,
            outcome_tier="NEUTRAL",
        )

    import app.plan_completion.metrics as metrics_mod

    monkeypatch.setattr(metrics_mod, "build_completion_metrics", fake_build_metrics)

    result = coach_agent._build_idle_finished_context(_DummyDB(plan), user_id=1)

    assert "adaptation_count" not in result
    assert "recommended_duration" not in result
    assert "recommended_load" not in result
    assert "recommended_focus" not in result


def test_build_idle_finished_context_returns_none_when_plan_missing():
    result = coach_agent._build_idle_finished_context(_DummyDB(plan=None), user_id=7)
    assert result is None


def test_build_idle_finished_context_returns_none_on_metrics_exception(monkeypatch):
    plan = SimpleNamespace(id=123, user_id=7, status="completed", end_date=datetime.now(timezone.utc))

    def fail_metrics(_db, _user_id, _plan_id):
        raise RuntimeError("boom")

    import app.plan_completion.metrics as metrics_mod

    monkeypatch.setattr(metrics_mod, "build_completion_metrics", fail_metrics)

    result = coach_agent._build_idle_finished_context(_DummyDB(plan), user_id=7)
    assert result is None


def test_context_message_includes_completion_context_when_present():
    payload = {
        "temporal_context": "2026-01-01T10:00:00Z",
        "current_state": "IDLE_FINISHED",
        "completion_context": {"total_days": 14, "completion_rate": 95},
    }

    message = coach_agent._context_message(payload)

    assert '"completion_context"' in message
    assert '"total_days": 14' in message
    assert '"completion_rate": 95' in message


def test_context_message_no_profile_snapshot():
    """profile_snapshot removed in T5.8C — must not appear in context block."""
    payload = {
        "temporal_context": "2026-01-01T10:00:00Z",
        "current_state": "IDLE_FINISHED",
    }
    message = coach_agent._context_message(payload)
    assert "profile_snapshot" not in message
    assert "user_profile" not in message


@pytest.mark.anyio
async def test_coach_agent_injects_completion_context_for_idle_finished(monkeypatch):
    captured = {}

    def fake_compose_messages(payload):
        captured["payload"] = payload
        return [{"role": "user", "content": "hi"}]

    async def fake_create(**_kwargs):
        return {"id": "resp"}

    class _Responses:
        create = staticmethod(fake_create)

    monkeypatch.setattr(coach_agent, "_compose_messages", fake_compose_messages)
    monkeypatch.setattr(coach_agent, "extract_output_text", lambda _response: "ok")
    monkeypatch.setattr(coach_agent, "_build_idle_finished_context", lambda _db, _uid: {"total_days": 10})
    monkeypatch.setattr(coach_agent.async_client, "responses", _Responses())

    class _Session:
        def __enter__(self):
            return object()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(coach_agent, "SessionLocal", lambda: _Session())

    payload = {"user_id": 42, "current_state": "IDLE_FINISHED", "message_text": "hey"}
    await coach_agent.coach_agent(payload)

    assert captured["payload"]["completion_context"] == {"total_days": 10}


@pytest.mark.anyio
async def test_coach_agent_does_not_inject_completion_context_for_other_states(monkeypatch):
    captured = {}

    def fake_compose_messages(payload):
        captured["payload"] = payload
        return [{"role": "user", "content": "hi"}]

    async def fake_create(**_kwargs):
        return {"id": "resp"}

    class _Responses:
        create = staticmethod(fake_create)

    monkeypatch.setattr(coach_agent, "_compose_messages", fake_compose_messages)
    monkeypatch.setattr(coach_agent, "extract_output_text", lambda _response: "ok")

    def fail_if_called(_db, _uid):
        raise AssertionError("_build_idle_finished_context should not be called")

    monkeypatch.setattr(coach_agent, "_build_idle_finished_context", fail_if_called)
    monkeypatch.setattr(coach_agent.async_client, "responses", _Responses())

    payload = {"user_id": 42, "current_state": "ACTIVE", "message_text": "hey"}
    await coach_agent.coach_agent(payload)

    assert "completion_context" not in captured["payload"]

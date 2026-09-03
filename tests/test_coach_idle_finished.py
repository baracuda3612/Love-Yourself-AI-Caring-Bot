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


@pytest.fixture
def anyio_backend():
    return "asyncio"


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


def test_build_idle_finished_context_ignores_older_completion_after_abandonment(
    monkeypatch,
):
    latest_plan = SimpleNamespace(id=124, user_id=7, status="abandoned")

    def fail_metrics(*_args):
        raise AssertionError("abandoned latest cycle must not expose old completion context")

    import app.plan_completion.metrics as metrics_mod

    monkeypatch.setattr(metrics_mod, "build_completion_metrics", fail_metrics)

    result = coach_agent._build_idle_finished_context(
        _DummyDB(latest_plan),
        user_id=7,
    )

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
        "current_mode": "NO_ACTIVE_PLAN",
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
        "current_mode": "NO_ACTIVE_PLAN",
    }
    message = coach_agent._context_message(payload)
    assert "profile_snapshot" not in message
    assert "user_profile" not in message


def test_compose_messages_injects_english_product_map_before_runtime_context():
    messages = coach_agent._compose_messages(
        {
            "current_mode": "ACTIVE",
            "temporal_context": "2026-01-01T10:00:00Z",
            "message_text": "Привіт",
        }
    )

    assert messages[0] == {"role": "system", "content": coach_agent.COACH_SYSTEM_PROMPT}
    assert messages[1] == {"role": "system", "content": coach_agent.COACH_PRODUCT_MAP}
    assert messages[1]["content"].startswith("# Love Yourself Product Map")
    assert messages[2]["role"] == "system"
    assert messages[2]["content"].startswith("Current runtime context")
    assert messages[-1] == {"role": "user", "content": "Привіт"}


@pytest.mark.parametrize(
    ("state", "expected_names"),
    [
        (
            "ACTIVE",
            {
                "pause_plan",
                "cancel_plan",
                "change_day_time",
                "change_evening_time",
                "get_plan_status",
            },
        ),
        (
            "ACTIVE_PAUSED",
            {
                "resume_plan",
                "cancel_plan",
                "change_day_time",
                "change_evening_time",
                "get_plan_status",
            },
        ),
        (
            "NO_ACTIVE_PLAN",
            {
                "create_followup_plan",
                "record_evening_time",
                "change_day_time",
                "change_evening_time",
                "get_plan_status",
            },
        ),
        ("NO_ACTIVE_PLAN_WITHOUT_HISTORY", set()),
        ("ONBOARDING", set()),
        ("UNKNOWN", set()),
    ],
)
def test_coach_tools_are_filtered_by_state(state, expected_names):
    actual_names = {
        tool["name"] for tool in coach_agent._coach_tools_for_state(state)
    }
    assert actual_names == expected_names


def test_coach_tool_schemas_are_strict_and_validate_hhmm_shape():
    for tool in coach_agent.COACH_TOOLS:
        assert tool["strict"] is True
        assert tool["parameters"]["additionalProperties"] is False

    hhmm_tools = {
        "record_evening_time",
        "change_day_time",
        "change_evening_time",
    }
    for tool in coach_agent.COACH_TOOLS:
        if tool["name"] in hhmm_tools:
            assert tool["parameters"]["properties"]["hhmm"]["pattern"] == (
                r"^(?:[01]\d|2[0-3]):[0-5]\d$"
            )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("state", "expected_names"),
    [
        (
            "ACTIVE",
            {
                "pause_plan",
                "cancel_plan",
                "change_day_time",
                "change_evening_time",
                "get_plan_status",
            },
        ),
        ("NO_ACTIVE_PLAN_WITHOUT_HISTORY", None),
    ],
)
async def test_coach_agent_sends_only_tools_available_in_current_state(
    monkeypatch,
    state,
    expected_names,
):
    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return {"id": "resp"}

    class _Responses:
        create = staticmethod(fake_create)

    monkeypatch.setattr(coach_agent.async_client, "responses", _Responses())
    monkeypatch.setattr(coach_agent, "extract_output_text", lambda _response: "ok")

    payload = {
        "user_id": 42,
        "current_mode": state,
        "message_text": "Привіт",
    }
    if state == "NO_ACTIVE_PLAN_WITHOUT_HISTORY":
        payload["completion_context"] = {}

    await coach_agent.coach_agent(payload)

    if expected_names is None:
        assert "tools" not in captured
    else:
        assert {tool["name"] for tool in captured["tools"]} == expected_names


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

    payload = {"user_id": 42, "current_mode": "NO_ACTIVE_PLAN", "message_text": "hey"}
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

    payload = {"user_id": 42, "current_mode": "ACTIVE", "message_text": "hey"}
    await coach_agent.coach_agent(payload)

    assert "completion_context" not in captured["payload"]

import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

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

from app import orchestrator


class DummyMemory:
    def __init__(self) -> None:
        self.messages = []

    async def append_message(self, user_id, role, text):  # pragma: no cover - helper
        self.messages.append((user_id, role, text))


@pytest.fixture(autouse=True)
def disable_auto_complete(monkeypatch):
    monkeypatch.setattr(orchestrator, "_auto_complete_plan_if_needed_for_user_id", lambda _user_id: None)


@pytest.mark.anyio
async def test_non_coach_agent_returns_immediately(monkeypatch):
    dummy_memory = DummyMemory()
    monkeypatch.setattr(orchestrator, "session_memory", dummy_memory)

    async def fake_build_user_context(user_id, message_text):
        return {"message_text": message_text, "current_state": "IDLE_ONBOARDED"}

    async def fake_call_router(user_id, message_text, context):
        return {
            "router_result": {
                "target_agent": "plan",
                "confidence": "HIGH",
                "intent_bucket": "STRUCTURAL",
            },
            "router_meta": {},
            "fsm_state": None,
            "session_id": None,
            "input_message": message_text,
            "context_payload": context,
        }

    invoke_calls = []

    async def fake_invoke_agent(target_agent, payload):
        invoke_calls.append(target_agent)
        return {"reply_text": f"{target_agent} says hi"}

    monkeypatch.setattr(orchestrator, "build_user_context", fake_build_user_context)
    monkeypatch.setattr(orchestrator, "call_router", fake_call_router)
    monkeypatch.setattr(orchestrator, "_invoke_agent", fake_invoke_agent)

    response = await orchestrator.handle_incoming_message(user_id=1, message_text="hello")

    assert response["reply_text"] == "plan says hi"
    assert invoke_calls == ["plan"]
    assert dummy_memory.messages == [
        (1, "user", "hello"),
        (1, "assistant", "plan says hi"),
    ]


@pytest.mark.anyio
async def test_coach_agent_allows_single_reroute(monkeypatch):
    dummy_memory = DummyMemory()
    monkeypatch.setattr(orchestrator, "session_memory", dummy_memory)

    async def fake_build_user_context(user_id, message_text):
        return {"message_text": message_text, "current_state": "IDLE_ONBOARDED"}

    async def fake_call_router(user_id, message_text, context):
        return {
            "router_result": {
                "target_agent": "coach",
                "confidence": "MEDIUM",
                "intent_bucket": "MEANING",
            },
            "router_meta": {},
            "fsm_state": None,
            "session_id": None,
            "input_message": message_text,
            "context_payload": context,
        }

    invoke_calls = []

    async def fake_invoke_agent(target_agent, payload):
        invoke_calls.append(target_agent)
        return {
            "reply_text": "coach response",
            "tool_calls": [
                {
                    "function": {
                        "name": "reroute_to_manager",
                        "arguments": {"target": "manager"},
                    }
                }
            ],
        }

    monkeypatch.setattr(orchestrator, "build_user_context", fake_build_user_context)
    monkeypatch.setattr(orchestrator, "call_router", fake_call_router)
    monkeypatch.setattr(orchestrator, "_invoke_agent", fake_invoke_agent)

    response = await orchestrator.handle_incoming_message(user_id=2, message_text="hi")

    assert response["reply_text"] == "coach response"
    assert invoke_calls == ["coach"]
    assert dummy_memory.messages == [
        (2, "user", "hi"),
        (2, "assistant", "coach response"),
    ]


@pytest.mark.anyio
async def test_coach_early_return_receives_completion_context_in_idle_finished(monkeypatch):
    dummy_memory = DummyMemory()
    monkeypatch.setattr(orchestrator, "session_memory", dummy_memory)

    async def fake_build_user_context(user_id, message_text):
        return {"message_text": message_text, "current_state": "IDLE_FINISHED"}

    async def fake_call_router(user_id, message_text, context):
        return {
            "router_result": {
                "target_agent": "coach",
                "confidence": "HIGH",
                "intent_bucket": "MEANING",
            },
            "router_meta": {},
            "fsm_state": None,
            "session_id": None,
            "input_message": message_text,
            "context_payload": context,
        }

    monkeypatch.setattr(orchestrator, "build_user_context", fake_build_user_context)
    monkeypatch.setattr(orchestrator, "call_router", fake_call_router)
    monkeypatch.setattr(
        orchestrator,
        "_build_idle_finished_context",
        lambda _db, _user_id: {"total_days": 14, "completion_rate": 80},
    )

    class _Session:
        def __enter__(self):
            return object()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(orchestrator, "SessionLocal", lambda: _Session())

    invoke_payloads = []

    async def fake_invoke_agent(target_agent, payload):
        invoke_payloads.append((target_agent, payload))
        return {"reply_text": "coach response"}

    monkeypatch.setattr(orchestrator, "_invoke_agent", fake_invoke_agent)

    response = await orchestrator.handle_incoming_message(user_id=21, message_text="hi")

    assert response["reply_text"] == "coach response"
    assert invoke_payloads[0][0] == "coach"
    assert invoke_payloads[0][1].get("completion_context") == {
        "total_days": 14,
        "completion_rate": 80,
    }


@pytest.mark.anyio
async def test_plan_tool_call_invokes_handler(monkeypatch):
    dummy_memory = DummyMemory()
    monkeypatch.setattr(orchestrator, "session_memory", dummy_memory)

    async def fake_build_user_context(user_id, message_text):
        return {"message_text": message_text, "current_state": "IDLE_ONBOARDED"}

    async def fake_call_router(user_id, message_text, context):
        return {
            "router_result": {
                "target_agent": "plan",
                "confidence": "HIGH",
                "intent_bucket": "STRUCTURAL",
            },
            "router_meta": {},
            "fsm_state": None,
            "session_id": None,
            "input_message": message_text,
            "context_payload": context,
        }

    async def fake_invoke_agent(target_agent, payload):
        return {"tool_call": {"name": "start_plan", "arguments": {}}}

    handler_calls = []

    def fake_run_plan_tool_call(tool_call):
        handler_calls.append(tool_call)
        return {"user_text": "Starting a plan. Tell me what you'd like to plan."}

    monkeypatch.setattr(orchestrator, "build_user_context", fake_build_user_context)
    monkeypatch.setattr(orchestrator, "call_router", fake_call_router)
    monkeypatch.setattr(orchestrator, "_invoke_agent", fake_invoke_agent)
    monkeypatch.setattr(orchestrator, "run_plan_tool_call", fake_run_plan_tool_call)

    response = await orchestrator.handle_incoming_message(user_id=3, message_text="Створи план")

    assert response["reply_text"] == "Starting a plan. Tell me what you'd like to plan."
    assert handler_calls == [{"name": "start_plan", "arguments": {}}]
    assert dummy_memory.messages == [
        (3, "user", "Створи план"),
        (3, "assistant", "Starting a plan. Tell me what you'd like to plan."),
    ]


class _FakeQuery:
    def __init__(self, steps):
        self._steps = steps

    def join(self, *_args, **_kwargs):
        return self

    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        return self._steps


class _FakeDB:
    def __init__(self, steps):
        self._steps = steps

    def query(self, *_args, **_kwargs):
        return _FakeQuery(self._steps)


class _FakeStep:
    def __init__(self, difficulty):
        self.difficulty = difficulty


class _FakePlan:
    def __init__(self, plan_id=1):
        self.id = plan_id


class _AutoCompleteQuery:
    def __init__(self, plans):
        self._plans = plans

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, n):
        self._plans = self._plans[:n]
        return self

    def all(self):
        return self._plans


class _AutoCompleteDB:
    def __init__(self, plans):
        self._plans = plans
        self.added = []

    def query(self, *_args, **_kwargs):
        return _AutoCompleteQuery(self._plans)

    def add(self, obj):
        self.added.append(obj)


def test_get_avg_difficulty_mixed_enum_values():
    db = _FakeDB([_FakeStep("EASY"), _FakeStep("MEDIUM"), _FakeStep("HARD")])
    plan = _FakePlan()

    result = orchestrator.get_avg_difficulty(db, plan)

    assert result == 2


def test_get_avg_difficulty_empty_steps_returns_default_one():
    db = _FakeDB([])
    plan = _FakePlan()

    result = orchestrator.get_avg_difficulty(db, plan)

    assert result == 1


def test_auto_complete_marks_plan_completed_and_logs_event_with_metrics_error(monkeypatch):
    user = type("UserStub", (), {})()
    user.id = 77
    user.current_state = "ACTIVE"
    user.plan_end_date = datetime.now(timezone.utc) - timedelta(days=1)

    latest_plan = type("PlanStub", (), {})()
    latest_plan.id = 9
    latest_plan.status = "active"
    latest_plan.created_at = datetime.now(timezone.utc)
    latest_plan.total_days = 14
    latest_plan.focus = "REST"
    latest_plan.load = "MID"
    latest_plan.duration = "MEDIUM"
    latest_plan.end_date = None

    db = _AutoCompleteDB([latest_plan])
    captured = {}

    def fake_log_user_event(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(orchestrator, "log_user_event", fake_log_user_event)
    def raise_no_loop():
        raise RuntimeError("no running loop")

    monkeypatch.setattr(orchestrator.asyncio, "get_running_loop", raise_no_loop)

    orchestrator._auto_complete_plan_if_needed(db, user)

    assert latest_plan.status == "completed"
    assert latest_plan.end_date is not None
    assert user.current_state == "IDLE_FINISHED"
    assert user.plan_end_date is None
    assert captured["event_type"] == "plan_completed"
    assert captured["context"]["plan_id"] == 9
    assert captured["context"]["metrics_error"] is True


def test_auto_complete_without_active_plan_sets_idle_without_logging(monkeypatch):
    user = type("UserStub", (), {})()
    user.id = 88
    user.current_state = "ACTIVE"
    user.plan_end_date = datetime.now(timezone.utc) - timedelta(days=1)

    db = _AutoCompleteDB([])
    called = {"value": False}

    def fake_log_user_event(**_kwargs):
        called["value"] = True

    monkeypatch.setattr(orchestrator, "log_user_event", fake_log_user_event)

    orchestrator._auto_complete_plan_if_needed(db, user)

    assert user.current_state == "IDLE_FINISHED"
    assert user.plan_end_date is None
    assert called["value"] is False


def test_auto_complete_reapplies_plan_and_user_after_event_logging_failure(monkeypatch):
    user = type("UserStub", (), {})()
    user.id = 101
    user.current_state = "ACTIVE"
    user.plan_end_date = datetime.now(timezone.utc) - timedelta(days=1)

    latest_plan = type("PlanStub", (), {})()
    latest_plan.id = 22
    latest_plan.status = "active"
    latest_plan.created_at = datetime.now(timezone.utc)
    latest_plan.total_days = 21
    latest_plan.focus = "REST"
    latest_plan.load = "MID"
    latest_plan.duration = "LONG"
    latest_plan.end_date = None

    db = _AutoCompleteDB([latest_plan])
    rollback_called = {"value": False}

    def fake_log_user_event(**_kwargs):
        raise RuntimeError("boom")

    def fake_rollback():
        rollback_called["value"] = True

    db.rollback = fake_rollback

    monkeypatch.setattr(orchestrator, "log_user_event", fake_log_user_event)

    orchestrator._auto_complete_plan_if_needed(db, user)

    assert rollback_called["value"] is True
    assert latest_plan.status == "completed"
    assert latest_plan.end_date is not None
    assert user.current_state == "IDLE_FINISHED"
    assert user.plan_end_date is None


def test_auto_complete_warns_and_uses_latest_plan(monkeypatch, caplog):
    user = type("UserStub", (), {})()
    user.id = 99
    user.current_state = "ACTIVE"
    user.plan_end_date = datetime.now(timezone.utc) - timedelta(days=1)

    latest_plan = type("PlanStub", (), {})()
    latest_plan.id = 10
    latest_plan.status = "active"
    latest_plan.created_at = datetime.now(timezone.utc)
    latest_plan.total_days = 7
    latest_plan.focus = "MIXED"
    latest_plan.load = "LITE"
    latest_plan.duration = "SHORT"
    latest_plan.end_date = None

    older_plan = type("PlanStub", (), {})()
    older_plan.id = 5
    older_plan.status = "active"
    older_plan.created_at = datetime.now(timezone.utc) - timedelta(days=10)
    older_plan.total_days = 7
    older_plan.focus = "REST"
    older_plan.load = "LITE"
    older_plan.duration = "SHORT"
    older_plan.end_date = None

    db = _AutoCompleteDB([latest_plan, older_plan])
    monkeypatch.setattr(orchestrator, "log_user_event", lambda **_kwargs: None)

    with caplog.at_level("WARNING"):
        orchestrator._auto_complete_plan_if_needed(db, user)

    assert latest_plan.status == "completed"
    assert older_plan.status == "active"
    assert "Multiple active plans found" in caplog.text


def test_get_avg_difficulty_unknown_value_falls_back_to_one():
    db = _FakeDB([_FakeStep("UNKNOWN"), _FakeStep("HARD")])
    plan = _FakePlan()

    result = orchestrator.get_avg_difficulty(db, plan)

    assert result == 2


@pytest.mark.anyio
async def test_plan_in_idle_new_returns_onboarding_guard(monkeypatch):
    dummy_memory = DummyMemory()
    monkeypatch.setattr(orchestrator, "session_memory", dummy_memory)

    async def fake_build_user_context(user_id, message_text):
        return {"message_text": message_text, "current_state": "IDLE_NEW"}

    async def fake_call_router(user_id, message_text, context):
        return {
            "router_result": {"target_agent": "plan", "confidence": "HIGH", "intent_bucket": "STRUCTURAL"},
            "router_meta": {},
            "fsm_state": "IDLE_NEW",
            "session_id": None,
            "input_message": message_text,
            "context_payload": context,
        }

    invoke_calls = []

    async def fake_invoke_agent(target_agent, payload):
        invoke_calls.append(target_agent)
        return {"reply_text": "unused"}

    monkeypatch.setattr(orchestrator, "build_user_context", fake_build_user_context)
    monkeypatch.setattr(orchestrator, "call_router", fake_call_router)
    monkeypatch.setattr(orchestrator, "_invoke_agent", fake_invoke_agent)

    response = await orchestrator.handle_incoming_message(user_id=10, message_text="створи план")

    assert response["reply_text"] == "Спочатку пройди вітальний процес. Напиши 'почати'."
    assert invoke_calls == []


@pytest.mark.anyio
async def test_plan_in_forbidden_state_falls_back_to_coach(monkeypatch):
    dummy_memory = DummyMemory()
    monkeypatch.setattr(orchestrator, "session_memory", dummy_memory)

    async def fake_build_user_context(user_id, message_text):
        return {"message_text": message_text, "current_state": "PLAN_FLOW:FINALIZATION"}

    async def fake_call_router(user_id, message_text, context):
        return {
            "router_result": {"target_agent": "plan", "confidence": "HIGH", "intent_bucket": "STRUCTURAL"},
            "router_meta": {},
            "fsm_state": "PLAN_FLOW:FINALIZATION",
            "session_id": None,
            "input_message": message_text,
            "context_payload": context,
        }

    invoke_calls = []

    async def fake_invoke_agent(target_agent, payload):
        invoke_calls.append(target_agent)
        return {"reply_text": "coach fallback"}

    monkeypatch.setattr(orchestrator, "build_user_context", fake_build_user_context)
    monkeypatch.setattr(orchestrator, "call_router", fake_call_router)
    monkeypatch.setattr(orchestrator, "_invoke_agent", fake_invoke_agent)

    response = await orchestrator.handle_incoming_message(user_id=11, message_text="план")

    assert response["reply_text"] == "coach fallback"
    assert invoke_calls == ["coach"]


@pytest.mark.anyio
async def test_adaptation_tunnel_plan_handled_before_plan_agent(monkeypatch):
    dummy_memory = DummyMemory()
    monkeypatch.setattr(orchestrator, "session_memory", dummy_memory)

    async def fake_build_user_context(user_id, message_text):
        return {"message_text": message_text, "current_state": "ADAPTATION_PARAMS"}

    async def fake_call_router(user_id, message_text, context):
        return {
            "router_result": {"target_agent": "plan", "confidence": "HIGH", "intent_bucket": "STRUCTURAL"},
            "router_meta": {},
            "fsm_state": "ADAPTATION_PARAMS",
            "session_id": None,
            "input_message": message_text,
            "context_payload": context,
        }

    async def fake_handle_adaptation_flow(user_id, message_text, current_state, db):
        assert current_state == "ADAPTATION_PARAMS"
        return "adaptation reply", ["followup"]

    async def fake_invoke_agent(target_agent, payload):
        raise AssertionError("_invoke_agent should not be called for early adaptation tunnel handling")

    class _DummySession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def commit(self):
            return None

        def rollback(self):
            return None

    monkeypatch.setattr(orchestrator, "build_user_context", fake_build_user_context)
    monkeypatch.setattr(orchestrator, "call_router", fake_call_router)
    monkeypatch.setattr(orchestrator, "handle_adaptation_flow", fake_handle_adaptation_flow)
    monkeypatch.setattr(orchestrator, "_invoke_agent", fake_invoke_agent)
    monkeypatch.setattr(orchestrator, "SessionLocal", lambda: _DummySession())

    response = await orchestrator.handle_incoming_message(user_id=12, message_text="cognitive")

    assert response["reply_text"] == "adaptation reply"
    assert response["followup_messages"] == ["followup"]

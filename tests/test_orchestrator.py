import os
import pathlib
import sys

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
    monkeypatch.setattr(orchestrator, "_auto_complete_plan_if_needed", lambda _user_id: None)


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

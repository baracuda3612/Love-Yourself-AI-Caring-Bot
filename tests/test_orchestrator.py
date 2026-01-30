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
        return {"message_text": message_text}

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

    reply = await orchestrator.handle_incoming_message(user_id=1, message_text="hello")

    assert reply == "plan says hi"
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
        return {"message_text": message_text}

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

    reply = await orchestrator.handle_incoming_message(user_id=2, message_text="hi")

    assert reply == "coach response"
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
        return {"message_text": message_text}

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

    reply = await orchestrator.handle_incoming_message(user_id=3, message_text="Створи план")

    assert reply == "Starting a plan. Tell me what you'd like to plan."
    assert handler_calls == [{"name": "start_plan", "arguments": {}}]
    assert dummy_memory.messages == [
        (3, "user", "Створи план"),
        (3, "assistant", "Starting a plan. Tell me what you'd like to plan."),
    ]

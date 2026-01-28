import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://test-user:test-pass@localhost:5432/test-db",
)
os.environ.setdefault("OPENAI_API_KEY", "test-key")

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app import ai_plans


class DummyResponse:
    def __init__(self, output=None, output_text: str = "") -> None:
        self.output = output or []
        self.output_text = output_text


@pytest.mark.anyio
async def test_plan_agent_sends_tools_and_auto_choice(monkeypatch):
    captured = {}

    async def fake_create(*_args, **kwargs):
        captured.update(kwargs)
        return DummyResponse(
            output=[{"type": "function_call", "name": "noop", "arguments": {}}]
        )

    monkeypatch.setattr(ai_plans.async_client.responses, "create", fake_create)

    payload = {"message_text": "I want to start a plan"}
    result = await ai_plans.generate_plan_agent_response(payload)

    assert result["tool_call"]["name"] == "noop"
    assert captured["tools"] == ai_plans._TOOL_DEFINITIONS
    assert captured["tool_choice"] == "auto"


def test_run_plan_tool_call_falls_back_to_noop():
    result = ai_plans.run_plan_tool_call({"name": "unknown"})

    assert result["user_text"] == "No action needed."

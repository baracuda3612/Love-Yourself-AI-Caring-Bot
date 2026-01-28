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

from app.ai_plans import _extract_tool_call


class DummyResponse:
    def __init__(self, output):
        self.output = output


def test_extract_tool_call_from_output_item():
    response = DummyResponse(
        output=[{"type": "function_call", "name": "start_plan", "arguments": {}}]
    )

    tool_call = _extract_tool_call(response)

    assert tool_call == {"name": "start_plan", "id": None, "arguments": {}}


def test_extract_tool_call_from_nested_content():
    response = DummyResponse(
        output=[
            {
                "type": "message",
                "content": [{"type": "tool_call", "name": "noop", "arguments": {}}],
            }
        ]
    )

    tool_call = _extract_tool_call(response)

    assert tool_call == {"name": "noop", "id": None, "arguments": {}}

import json
import os

import pytest

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://test-user:test-pass@localhost:5432/test-db",
)
os.environ.setdefault("OPENAI_API_KEY", "test-key")

from app.ai import async_client
from app.ai_router import cognitive_route_message


class DummyMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class DummyChoice:
    def __init__(self, content: str) -> None:
        self.message = DummyMessage(content)


class DummyUsage:
    def __init__(self, prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class DummyResponse:
    def __init__(self, content: str) -> None:
        self.choices = [DummyChoice(content)]
        self.usage = DummyUsage()


test_cases = [
    {
        "input": {
            "user_id": 1,
            "current_state": "ACTIVE",
            "latest_user_message": "I want to end it all",
            "short_term_history": [],
        },
        "expected": {
            "target_agent": "safety",
            "confidence": "HIGH",
            "intent_bucket": "SAFETY",
        },
    },
    {
        "input": {
            "user_id": 2,
            "current_state": "IDLE_NEW",
            "latest_user_message": "Hi",
            "short_term_history": [],
        },
        "expected": {
            "target_agent": "onboarding",
        },
    },
    {
        "input": {
            "user_id": 3,
            "current_state": "PLAN_FLOW:DATA_COLLECTION",
            "latest_user_message": "21 days",
            "short_term_history": [
                {"role": "assistant", "content": "Choose duration: 7, 21, or 90 days?"},
                {"role": "user", "content": "21 days"},
            ],
        },
        "expected": {
            "target_agent": "plan",
            "confidence": "HIGH",
            "intent_bucket": "STRUCTURAL",
        },
    },
    {
        "input": {
            "user_id": 4,
            "current_state": "PLAN_FLOW:DATA_COLLECTION",
            "latest_user_message": "I don't know which to choose",
            "short_term_history": [
                {"role": "assistant", "content": "Choose duration: 7, 21, or 90 days?"},
                {"role": "user", "content": "I don't know which to choose"},
            ],
        },
        "expected": {
            "target_agent": "coach",
            "confidence": "HIGH",
            "intent_bucket": "MEANING",
        },
    },
    {
        "input": {
            "user_id": 5,
            "current_state": "ACTIVE",
            "latest_user_message": "I want to change my plan",
            "short_term_history": [],
        },
        "expected": {
            "target_agent": "plan",
            "confidence": "HIGH",
            "intent_bucket": "STRUCTURAL",
        },
    },
    {
        "input": {
            "user_id": 6,
            "current_state": "ACTIVE",
            "latest_user_message": "hmm",
            "short_term_history": [],
        },
        "expected": {
            "target_agent": "coach",
            "confidence": "LOW",
            "intent_bucket": "UNKNOWN",
        },
    },
]


@pytest.mark.anyio
@pytest.mark.parametrize("case", test_cases)
async def test_router_contract(monkeypatch, case):
    expected = case["expected"]
    response_payload = {
        "target_agent": expected.get("target_agent", "coach"),
        "confidence": expected.get("confidence", "LOW"),
        "intent_bucket": expected.get("intent_bucket", "UNKNOWN"),
    }

    async def fake_create(*_args, **kwargs):
        messages = kwargs.get("messages") or []
        assert messages
        user_payload = json.loads(messages[-1]["content"])
        assert user_payload["latest_user_message"] == case["input"]["latest_user_message"]
        return DummyResponse(json.dumps(response_payload))

    monkeypatch.setattr(async_client.chat.completions, "create", fake_create)

    result = await cognitive_route_message(case["input"])
    router_result = result["router_result"]

    for key, value in expected.items():
        assert router_result.get(key) == value

    assert router_result["confidence"] in {"HIGH", "MEDIUM", "LOW"}
    assert router_result["intent_bucket"] in {"SAFETY", "STRUCTURAL", "MEANING", "UNKNOWN"}

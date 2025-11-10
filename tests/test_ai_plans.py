import json
import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app import ai_plans
from app.telegram import _coerce_plan_payload


class DummyMessage:
    def __init__(self, content: str):
        self.content = content


class DummyChoice:
    def __init__(self, content: str):
        self.message = DummyMessage(content)


class DummyResponse:
    def __init__(self, content: str):
        self.choices = [DummyChoice(content)]


class DummyCompletions:
    def __init__(self, content: str):
        self._content = content

    def create(self, *args, **kwargs):
        return DummyResponse(self._content)


class DummyChat:
    def __init__(self, content: str):
        self.completions = DummyCompletions(content)


class DummyClient:
    def __init__(self, content: str):
        self.chat = DummyChat(content)


@pytest.fixture(autouse=True)
def restore_client():
    original = ai_plans._client
    yield
    ai_plans._client = original


def test_generate_ai_plan_valid_json():
    payload = {
        "plan_name": "Тестовий план",
        "steps": [
            {"day": 1, "message": "Крок один", "scheduled_for": "2024-01-01T10:00:00"}
        ],
    }
    ai_plans._client = DummyClient(json.dumps(payload, ensure_ascii=False))

    plan = ai_plans.generate_ai_plan(
        goal="Покращити сон",
        days=3,
        tasks_per_day=1,
        preferred_hour="21:00",
        tz_name="Europe/Kyiv",
        memory=None,
    )

    assert plan["plan_name"] == "Тестовий план"
    assert plan["steps"] == payload["steps"]


def test_coerce_plan_payload_extracts_embedded_json():
    raw = "Ось відповідь: ```json {\"plan_name\":\"A\",\"steps\":[{\"day\":1,\"message\":\"Hi\"}]} ```"
    result = _coerce_plan_payload(raw)
    assert result["plan_name"] == "A"
    assert result["steps"][0]["message"] == "Hi"


def test_generate_ai_plan_fallback_used_when_invalid_response():
    ai_plans._client = DummyClient("Невдала відповідь без JSON")

    plan = ai_plans.generate_ai_plan(
        goal="Покращити сон",
        days=3,
        tasks_per_day=1,
        preferred_hour="21:00",
        tz_name="Europe/Kyiv",
        memory=None,
    )

    assert plan["plan_name"] == "План для Покращити сон"
    assert len(plan["steps"]) == len(ai_plans.PLAYBOOKS["сон"])
    assert plan["steps"][0]["day"] == 1

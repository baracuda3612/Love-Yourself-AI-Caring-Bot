from __future__ import annotations

import os
import pathlib
import sys

import pytest

os.environ.setdefault("BOT_TOKEN", "123456:TESTTOKEN")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://test-user:test-pass@localhost:5432/test-db",
)
os.environ.setdefault("OPENAI_API_KEY", "test-key")

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

from app import orchestrator
from datetime import time


class _DummySessionMemory:
    def __init__(self, ctx):
        self.ctx = ctx

    async def get_schedule_adjustment_context(self, _user_id):
        return self.ctx

    async def update_schedule_adjustment_context(self, _user_id, updates):
        self.ctx.update(updates)

    async def set_schedule_adjustment_last_active(self, _user_id):
        return None



def test_infer_slot_morning():
    assert orchestrator.infer_slot(time(7, 30)) == "MORNING"


def test_infer_slot_evening():
    assert orchestrator.infer_slot(time(21, 0)) == "EVENING"


def test_infer_slot_out_of_range():
    assert orchestrator.infer_slot(time(3, 0)) is None


@pytest.mark.anyio
async def test_record_same_slot_no_conflict(monkeypatch):
    memory = _DummySessionMemory(
        {
            "active_tasks": {"MORNING": "08:00", "EVENING": "20:00"},
            "slots_queue": ["MORNING"],
            "current_slot": "MORNING",
            "pending_changes": {},
            "step": "time_select",
        }
    )
    monkeypatch.setattr(orchestrator, "session_memory", memory)

    result = await orchestrator._handle_schedule_adjustment_record(
        1,
        {"new_time": "07:30", "user_text": "ok"},
        None,
    )

    assert result["user_text"] == "ok"
    assert memory.ctx["pending_changes"]["MORNING"]["new_slot"] == "MORNING"


@pytest.mark.anyio
async def test_record_cross_slot_conflict(monkeypatch):
    memory = _DummySessionMemory(
        {
            "active_tasks": {"MORNING": "08:00", "EVENING": "20:00"},
            "slots_queue": ["EVENING"],
            "current_slot": "EVENING",
            "pending_changes": {},
            "step": "time_select",
        }
    )
    monkeypatch.setattr(orchestrator, "session_memory", memory)

    result = await orchestrator._handle_schedule_adjustment_record(
        1,
        {"new_time": "09:00", "user_text": "ok"},
        None,
    )

    assert "У цей час вже є інше завдання" in result["user_text"]


@pytest.mark.anyio
async def test_record_cross_slot_free(monkeypatch):
    memory = _DummySessionMemory(
        {
            "active_tasks": {"MORNING": "08:00"},
            "slots_queue": ["MORNING"],
            "current_slot": "MORNING",
            "pending_changes": {},
            "step": "time_select",
        }
    )
    monkeypatch.setattr(orchestrator, "session_memory", memory)

    await orchestrator._handle_schedule_adjustment_record(
        1,
        {"new_time": "18:00", "user_text": "ok"},
        None,
    )

    assert "MORNING" not in memory.ctx["active_tasks"]
    assert memory.ctx["active_tasks"]["EVENING"] == "18:00"
    assert memory.ctx["pending_changes"]["MORNING"]["new_slot"] == "EVENING"

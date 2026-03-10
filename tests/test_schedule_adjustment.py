from __future__ import annotations

import os
import pathlib
import sys
from datetime import datetime, time, timezone
from types import SimpleNamespace

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

from app import orchestrator, telegram
from app.fsm.guards import can_transition


class _DummySessionMemory:
    def __init__(self, ctx):
        self.ctx = ctx

    async def get_schedule_adjustment_context(self, _user_id):
        return self.ctx

    async def update_schedule_adjustment_context(self, _user_id, updates):
        self.ctx.update(updates)

    async def set_schedule_adjustment_last_active(self, _user_id):
        return None

    async def clear_schedule_adjustment_context(self, _user_id):
        return None

    async def clear_schedule_adjustment_last_active(self, _user_id):
        return None

    async def clear_schedule_adjustment_soft_prompted(self, _user_id):
        return None


class _DummyQuery:
    def __init__(self, row):
        self._row = row

    def filter(self, *_args, **_kwargs):
        return self

    def join(self, *_args, **_kwargs):
        return self

    def all(self):
        return []

    def first(self):
        return self._row


class _DummyDB:
    def __init__(self, user):
        self.user = user
        self.commits = 0

    def query(self, model):
        if model is orchestrator.User:
            return _DummyQuery(self.user)
        return _DummyQuery(None)

    def add(self, _obj):
        return None

    def commit(self):
        self.commits += 1


class _SessionLocalCtx:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _Message:
    def __init__(self):
        self.answers = []
        self.edits = []

    async def edit_reply_markup(self, reply_markup=None):
        self.edits.append(reply_markup)

    async def answer(self, text, reply_markup=None):
        self.answers.append((text, reply_markup))


def _flatten_keyboard_texts(markup):
    return [btn.text for row in markup.inline_keyboard for btn in row]


def test_infer_slot_morning():
    assert orchestrator.infer_slot(time(7, 30)) == "MORNING"


def test_infer_slot_evening():
    assert orchestrator.infer_slot(time(21, 0)) == "EVENING"


def test_infer_slot_out_of_range():
    assert orchestrator.infer_slot(time(3, 0)) is None


def test_time_keyboard_single_plan_no_only_this():
    keyboard = orchestrator._build_time_select_keyboard("MORNING", "08:00", in_multi=False)
    texts = _flatten_keyboard_texts(keyboard)
    assert "✓ Тільки це завдання" not in texts
    assert "❌ Скасувати зміни" in texts


def test_time_keyboard_multi_mode_has_only_this():
    keyboard = orchestrator._build_time_select_keyboard("MORNING", "08:00", in_multi=True)
    texts = _flatten_keyboard_texts(keyboard)
    assert "✓ Тільки це завдання" in texts
    assert "❌ Скасувати зміни" in texts


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
async def test_record_cross_slot_returns_range_explanation(monkeypatch):
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
        {"new_time": "19:00", "user_text": "ok"},
        None,
    )

    assert "між 06:00–11:59" in result["user_text"]
    assert memory.ctx["pending_changes"] == {}


@pytest.mark.anyio
async def test_single_task_select_clears_queue(monkeypatch):
    user = SimpleNamespace(id=11)
    memory = _DummySessionMemory(
        {
            "active_tasks": {"MORNING": "08:00", "EVENING": "20:00"},
            "slots_queue": ["MORNING", "EVENING"],
            "current_slot": None,
            "step": "task_select",
        }
    )
    monkeypatch.setattr(telegram, "session_memory", memory)
    monkeypatch.setattr(telegram, "SessionLocal", _SessionLocalCtx)
    monkeypatch.setattr(telegram, "_ensure_user", lambda _db, _tg_user: (user, False))

    msg = _Message()
    answered = []

    async def _answer():
        answered.append(True)

    cb = SimpleNamespace(
        data="sched_task:MORNING",
        from_user=SimpleNamespace(id=999),
        message=msg,
        answer=_answer,
    )

    await telegram.on_sched_adj_task(cb)

    assert memory.ctx["slots_queue"] == []
    assert memory.ctx["current_slot"] == "MORNING"
    assert answered


@pytest.mark.anyio
async def test_only_this_clears_queue(monkeypatch):
    user = SimpleNamespace(id=12)
    memory = _DummySessionMemory(
        {
            "active_tasks": {"MORNING": "08:00", "EVENING": "20:00"},
            "slots_queue": ["MORNING", "EVENING"],
            "current_slot": "MORNING",
            "step": "time_select",
        }
    )
    monkeypatch.setattr(telegram, "session_memory", memory)
    monkeypatch.setattr(telegram, "SessionLocal", _SessionLocalCtx)
    monkeypatch.setattr(telegram, "_ensure_user", lambda _db, _tg_user: (user, False))

    msg = _Message()

    async def _answer():
        return None

    cb = SimpleNamespace(
        data="sched_time:MORNING:ONLY_THIS",
        from_user=SimpleNamespace(id=999),
        message=msg,
        answer=_answer,
    )

    await telegram.on_sched_adj_time(cb)

    assert memory.ctx["slots_queue"] == []
    assert msg.answers
    assert "тільки це завдання" in msg.answers[0][0].lower()


def test_paused_user_can_enter_tunnel():
    assert can_transition("ACTIVE_PAUSED", "SCHEDULE_ADJUSTMENT") is True


@pytest.mark.anyio
async def test_paused_user_cancel_returns_to_paused(monkeypatch):
    memory = _DummySessionMemory({"plan_was_paused": True})
    monkeypatch.setattr(orchestrator, "session_memory", memory)

    called = {}

    async def _fake_commit_fsm_transition(**kwargs):
        called["next_state"] = kwargs.get("next_state")

    monkeypatch.setattr(orchestrator, "_commit_fsm_transition", _fake_commit_fsm_transition)

    await orchestrator._handle_schedule_adjustment_cancel(
        user_id=1,
        tool_args={"user_text": "ok"},
        db=object(),
    )

    assert called["next_state"] == "ACTIVE_PAUSED"


@pytest.mark.anyio
async def test_apply_reads_profile_daily_time_slots(monkeypatch):
    profile = SimpleNamespace(daily_time_slots={"MORNING": "08:10", "DAY": "13:00", "EVENING": "20:00"})
    user = SimpleNamespace(id=1, profile=profile, timezone="Europe/Kyiv")
    plan = SimpleNamespace(id=42, current_day=1, start_date=datetime.now(timezone.utc), status="active")

    memory = _DummySessionMemory(
        {
            "pending_changes": {
                "MORNING": {"new_time": "07:30", "new_slot": "MORNING"},
            },
            "plan_was_paused": False,
        }
    )
    db = _DummyDB(user)
    monkeypatch.setattr(orchestrator, "session_memory", memory)

    def _fake_get_active_plan(db_arg, user_id_arg):
        assert db_arg is db
        assert user_id_arg == 1
        return plan

    monkeypatch.setattr(orchestrator, "get_active_plan", _fake_get_active_plan)
    monkeypatch.setattr(orchestrator, "log_user_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "reschedule_plan_steps", lambda *_args, **_kwargs: None)

    async def _fake_commit_fsm_transition(**_kwargs):
        return None

    monkeypatch.setattr(orchestrator, "_commit_fsm_transition", _fake_commit_fsm_transition)

    result = await orchestrator._handle_schedule_adjustment_apply(1, {"user_text": "done"}, db)

    assert result["user_text"] == "done"
    assert profile.daily_time_slots["MORNING"] == "07:30"


@pytest.mark.anyio
async def test_apply_no_changes_paused_returns_to_paused(monkeypatch):
    profile = SimpleNamespace(daily_time_slots={"MORNING": "08:10", "DAY": "13:00", "EVENING": "20:00"})
    user = SimpleNamespace(id=1, profile=profile, timezone="Europe/Kyiv")
    plan = SimpleNamespace(id=99, current_day=1, start_date=datetime.now(timezone.utc), status="paused")

    memory = _DummySessionMemory({"pending_changes": {}, "plan_was_paused": True})
    db = _DummyDB(user)
    monkeypatch.setattr(orchestrator, "session_memory", memory)
    monkeypatch.setattr(orchestrator, "get_active_plan", lambda _db, _user_id: plan)

    called = {}

    async def _fake_commit_fsm_transition(**kwargs):
        called["next_state"] = kwargs.get("next_state")
        return None

    monkeypatch.setattr(orchestrator, "_commit_fsm_transition", _fake_commit_fsm_transition)

    result = await orchestrator._handle_schedule_adjustment_apply(1, {}, db)

    assert called["next_state"] == "ACTIVE_PAUSED"
    assert result["user_text"] == "Нічого не змінилось."


@pytest.mark.anyio
async def test_apply_with_changes_paused_stays_paused(monkeypatch):
    profile = SimpleNamespace(daily_time_slots={"MORNING": "08:10", "DAY": "13:00", "EVENING": "20:00"})
    user = SimpleNamespace(id=1, profile=profile, timezone="Europe/Kyiv")
    plan = SimpleNamespace(id=77, current_day=1, start_date=datetime.now(timezone.utc), status="paused")

    memory = _DummySessionMemory(
        {
            "pending_changes": {
                "MORNING": {"new_time": "07:00", "new_slot": "MORNING"},
            },
            "plan_was_paused": True,
        }
    )
    db = _DummyDB(user)
    monkeypatch.setattr(orchestrator, "session_memory", memory)
    monkeypatch.setattr(orchestrator, "get_active_plan", lambda _db, _user_id: plan)
    monkeypatch.setattr(orchestrator, "log_user_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "reschedule_plan_steps", lambda *_args, **_kwargs: None)

    called = {}

    async def _fake_commit_fsm_transition(**kwargs):
        called["next_state"] = kwargs.get("next_state")
        return None

    monkeypatch.setattr(orchestrator, "_commit_fsm_transition", _fake_commit_fsm_transition)

    result = await orchestrator._handle_schedule_adjustment_apply(1, {}, db)

    assert called["next_state"] == "ACTIVE_PAUSED"
    assert "План відновлено" not in result["user_text"]


@pytest.mark.anyio
async def test_apply_with_changes_active_goes_active(monkeypatch):
    profile = SimpleNamespace(daily_time_slots={"MORNING": "08:10", "DAY": "13:00", "EVENING": "20:00"})
    user = SimpleNamespace(id=1, profile=profile, timezone="Europe/Kyiv")
    plan = SimpleNamespace(id=88, current_day=1, start_date=datetime.now(timezone.utc), status="active")

    memory = _DummySessionMemory(
        {
            "pending_changes": {
                "MORNING": {"new_time": "07:00", "new_slot": "MORNING"},
            },
            "plan_was_paused": False,
        }
    )
    db = _DummyDB(user)
    monkeypatch.setattr(orchestrator, "session_memory", memory)
    monkeypatch.setattr(orchestrator, "get_active_plan", lambda _db, _user_id: plan)
    monkeypatch.setattr(orchestrator, "log_user_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "reschedule_plan_steps", lambda *_args, **_kwargs: None)

    called = {}

    async def _fake_commit_fsm_transition(**kwargs):
        called["next_state"] = kwargs.get("next_state")
        return None

    monkeypatch.setattr(orchestrator, "_commit_fsm_transition", _fake_commit_fsm_transition)

    await orchestrator._handle_schedule_adjustment_apply(1, {}, db)

    assert called["next_state"] == "ACTIVE"


@pytest.mark.anyio
async def test_timeout_reset_callback_paused_returns_to_paused(monkeypatch):
    user = SimpleNamespace(id=15, tg_id=115, current_state="SCHEDULE_ADJUSTMENT")
    memory = _DummySessionMemory({"plan_was_paused": True})

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def add(self, _obj):
            return None

        def commit(self):
            return None

    monkeypatch.setattr(telegram, "session_memory", memory)
    monkeypatch.setattr(telegram, "SessionLocal", lambda: _Session())
    monkeypatch.setattr(telegram, "_ensure_user", lambda _db, _tg_user: (user, False))

    sent = []

    async def _send_message(chat_id, text):
        sent.append((chat_id, text))

    monkeypatch.setattr(telegram.bot, "send_message", _send_message)

    msg = _Message()

    async def _answer():
        return None

    cb = SimpleNamespace(
        data="sched_adj_timeout_reset",
        from_user=SimpleNamespace(id=999),
        message=msg,
        answer=_answer,
    )

    await telegram.on_sched_adj_timeout(cb)

    assert user.current_state == "ACTIVE_PAUSED"

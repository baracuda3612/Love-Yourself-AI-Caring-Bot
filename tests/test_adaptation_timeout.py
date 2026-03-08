from __future__ import annotations

import asyncio

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import os
import pathlib
import sys

os.environ.setdefault("BOT_TOKEN", "123456:TESTTOKEN")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://test-user:test-pass@localhost:5432/test-db",
)
os.environ.setdefault("OPENAI_API_KEY", "test-key")

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

from app import scheduler, telegram


class _DummySessionMemory:
    def __init__(self, last_active=None, prompted=False):
        self.last_active = last_active
        self.prompted = prompted
        self.calls: list[tuple] = []

    async def get_adaptation_last_active(self, user_id):
        self.calls.append(("get_last_active", user_id))
        return self.last_active

    async def set_adaptation_last_active(self, user_id):
        self.calls.append(("set_last_active", user_id))
        self.last_active = datetime.utcnow()

    async def get_adaptation_soft_prompted(self, user_id):
        self.calls.append(("get_soft_prompted", user_id))
        return self.prompted

    async def set_adaptation_soft_prompted(self, user_id):
        self.calls.append(("set_soft_prompted", user_id))
        self.prompted = True

    async def clear_adaptation_soft_prompted(self, user_id):
        self.calls.append(("clear_soft_prompted", user_id))
        self.prompted = False

    async def clear_adaptation_last_active(self, user_id):
        self.calls.append(("clear_last_active", user_id))

    async def clear_adaptation_context(self, user_id):
        self.calls.append(("clear_context", user_id))


class _DummyDB:
    def __init__(self, users):
        self._users = users
        self.committed = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def query(self, _model):
        return _DummyQuery(self._users)

    def add(self, _obj):
        return None

    def commit(self):
        self.committed += 1


class _DummyQuery:
    def __init__(self, users):
        self._users = users

    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        return self._users


class _DummyBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, tg_id, text, reply_markup=None):
        self.sent.append((tg_id, text, reply_markup))


def _run_check(monkeypatch, users, memory):
    sent = []
    reset = []

    async def fake_send(user, _bot):
        sent.append(user.id)

    async def fake_reset(user, _db):
        reset.append(user.id)

    class _DummyLoop:
        pass

    monkeypatch.setattr(scheduler, "_event_loop", _DummyLoop())
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: _DummyDB(users))
    monkeypatch.setattr(scheduler, "_send_adaptation_timeout_prompt", fake_send)
    monkeypatch.setattr(scheduler, "_force_reset_adaptation", fake_reset)

    async def _fake_run_coroutine_threadsafe(coro, _loop):
        await coro

    def _runner(coro, _loop):
        import asyncio

        return asyncio.get_event_loop().create_task(coro)

    monkeypatch.setattr(scheduler.asyncio, "run_coroutine_threadsafe", _runner)

    import sys

    module = SimpleNamespace(session_memory=memory)
    monkeypatch.setitem(sys.modules, "app.session_memory", module)
    monkeypatch.setitem(
        sys.modules,
        "app.fsm.states",
        SimpleNamespace(ADAPTATION_FLOW_STATES={"ADAPTATION_SELECTION"}),
    )
    monkeypatch.setitem(sys.modules, "app.telegram", SimpleNamespace(bot=_DummyBot()))

    scheduler.check_stuck_adaptations()
    return sent, reset


@pytest.mark.anyio
async def test_soft_prompt_sent_after_30min(monkeypatch):
    user = SimpleNamespace(id=1, tg_id=100, current_state="ADAPTATION_SELECTION")
    memory = _DummySessionMemory(last_active=datetime.utcnow() - timedelta(minutes=31), prompted=False)
    sent, reset = _run_check(monkeypatch, [user], memory)
    await asyncio.sleep(0)
    assert sent == [1]
    assert reset == []


@pytest.mark.anyio
async def test_soft_prompt_sent_only_once(monkeypatch):
    user = SimpleNamespace(id=2, tg_id=101, current_state="ADAPTATION_SELECTION")
    memory = _DummySessionMemory(last_active=datetime.utcnow() - timedelta(minutes=40), prompted=True)
    sent, reset = _run_check(monkeypatch, [user], memory)
    await asyncio.sleep(0)
    assert sent == []
    assert reset == []


@pytest.mark.anyio
async def test_hard_reset_after_60min_with_prompt(monkeypatch):
    user = SimpleNamespace(id=3, tg_id=102, current_state="ADAPTATION_SELECTION")
    memory = _DummySessionMemory(last_active=datetime.utcnow() - timedelta(minutes=61), prompted=True)
    sent, reset = _run_check(monkeypatch, [user], memory)
    await asyncio.sleep(0)
    assert sent == []
    assert reset == [3]


@pytest.mark.anyio
async def test_no_hard_reset_without_soft_prompt(monkeypatch):
    user = SimpleNamespace(id=4, tg_id=103, current_state="ADAPTATION_SELECTION")
    memory = _DummySessionMemory(last_active=datetime.utcnow() - timedelta(minutes=61), prompted=False)
    sent, reset = _run_check(monkeypatch, [user], memory)
    await asyncio.sleep(0)
    assert sent == [4]
    assert reset == []


@pytest.mark.anyio
async def test_no_action_if_active_user(monkeypatch):
    user = SimpleNamespace(id=5, tg_id=104, current_state="ACTIVE")
    memory = _DummySessionMemory(last_active=datetime.utcnow() - timedelta(minutes=80), prompted=True)
    sent, reset = _run_check(monkeypatch, [], memory)
    await asyncio.sleep(0)
    assert sent == []
    assert reset == []


@pytest.mark.anyio
async def test_fallback_sets_timer_on_missing_redis(monkeypatch):
    user = SimpleNamespace(id=6, tg_id=105, current_state="ADAPTATION_SELECTION")
    memory = _DummySessionMemory(last_active=None, prompted=False)
    sent, reset = _run_check(monkeypatch, [user], memory)
    await asyncio.sleep(0)
    assert sent == []
    assert reset == []
    assert ("set_last_active", 6) in memory.calls


@pytest.mark.anyio
async def test_continue_resets_timer(monkeypatch):
    memory = _DummySessionMemory(last_active=datetime.utcnow(), prompted=True)

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    user = SimpleNamespace(id=7, tg_id=106, current_state="ADAPTATION_SELECTION")
    monkeypatch.setattr(telegram, "SessionLocal", lambda: _Session())
    monkeypatch.setattr(telegram, "_ensure_user", lambda _db, _from_user: (user, False))

    import sys

    monkeypatch.setitem(sys.modules, "app.session_memory", SimpleNamespace(session_memory=memory))

    edited = []

    class _Message:
        async def edit_reply_markup(self, reply_markup=None):
            edited.append(reply_markup)

    answered = []

    async def _answer():
        answered.append(True)

    cb = SimpleNamespace(
        data="adaptation_timeout_continue",
        from_user=SimpleNamespace(id=999),
        message=_Message(),
        answer=_answer,
    )

    await telegram.on_adaptation_timeout_action(cb)

    assert ("set_last_active", 7) in memory.calls
    assert ("clear_soft_prompted", 7) in memory.calls
    assert answered
    assert edited == [None]


@pytest.mark.anyio
async def test_yes_resets_fsm_to_active(monkeypatch):
    memory = _DummySessionMemory(last_active=datetime.utcnow(), prompted=True)

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def add(self, _obj):
            return None

        def commit(self):
            return None

    user = SimpleNamespace(id=8, tg_id=107, current_state="ADAPTATION_SELECTION")
    monkeypatch.setattr(telegram, "SessionLocal", lambda: _Session())
    monkeypatch.setattr(telegram, "_ensure_user", lambda _db, _from_user: (user, False))

    sent = []

    async def _send_message(chat_id, text):
        sent.append((chat_id, text))

    monkeypatch.setattr(telegram.bot, "send_message", _send_message)

    import sys

    monkeypatch.setitem(sys.modules, "app.session_memory", SimpleNamespace(session_memory=memory))

    class _Message:
        async def edit_reply_markup(self, reply_markup=None):
            return None

    async def _answer():
        return None

    cb = SimpleNamespace(
        data="adaptation_timeout_reset",
        from_user=SimpleNamespace(id=999),
        message=_Message(),
        answer=_answer,
    )

    await telegram.on_adaptation_timeout_action(cb)

    assert user.current_state == "ACTIVE"
    assert ("clear_context", 8) in memory.calls
    assert sent and sent[0][0] == 107

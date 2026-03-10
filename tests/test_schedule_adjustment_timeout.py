from __future__ import annotations

import asyncio
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone
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

from app import scheduler


class _DummySessionMemory:
    def __init__(self, last_active=None, prompted=False):
        self.last_active = last_active
        self.prompted = prompted

    async def get_schedule_adjustment_last_active(self, _user_id):
        return self.last_active

    async def set_schedule_adjustment_last_active(self, _user_id):
        self.last_active = datetime.now(timezone.utc)

    async def get_schedule_adjustment_soft_prompted(self, _user_id):
        return self.prompted


class _DummyDB:
    def __init__(self, users):
        self._users = users

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def query(self, _model):
        return _DummyQuery(self._users)


class _DummyQuery:
    def __init__(self, users):
        self.users = users

    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        return self.users


@pytest.mark.anyio
async def test_soft_prompt_after_idle(monkeypatch):
    user = SimpleNamespace(id=1, tg_id=100, current_state="SCHEDULE_ADJUSTMENT")
    memory = _DummySessionMemory(
        last_active=datetime.now(timezone.utc) - timedelta(minutes=16),
        prompted=False,
    )

    sent = []

    async def fake_send(u, _bot):
        sent.append(u.id)

    class _DummyLoop:
        pass

    monkeypatch.setattr(scheduler, "_event_loop", _DummyLoop())
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: _DummyDB([user]))
    monkeypatch.setattr(scheduler, "_send_schedule_adjustment_timeout_prompt", fake_send)
    monkeypatch.setattr(scheduler, "_force_reset_schedule_adjustment", lambda *_: None)
    monkeypatch.setattr(
        scheduler.asyncio,
        "run_coroutine_threadsafe",
        lambda coro, _loop: asyncio.get_event_loop().create_task(coro),
    )

    import sys

    monkeypatch.setitem(sys.modules, "app.session_memory", SimpleNamespace(session_memory=memory))
    monkeypatch.setitem(sys.modules, "app.telegram", SimpleNamespace(bot=SimpleNamespace()))

    scheduler.check_stuck_schedule_adjustments()
    await asyncio.sleep(0)

    assert sent == [1]

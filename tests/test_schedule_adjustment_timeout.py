from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from app import scheduler


class _DummySessionMemory:
    def __init__(self, context=None):
        self.context = context or {}
        self.last_active = object()
        self.prompted = True

    async def get_schedule_adjustment_context(self, _user_id):
        return self.context

    async def clear_schedule_adjustment_context(self, _user_id):
        self.context = {}

    async def clear_schedule_adjustment_last_active(self, _user_id):
        self.last_active = None

    async def clear_schedule_adjustment_soft_prompted(self, _user_id):
        self.prompted = False


def test_stored_fsm_timeout_scanner_is_inert(monkeypatch):
    def _must_not_open_session():
        raise AssertionError("legacy current_state scanner must stay disabled")

    monkeypatch.setattr(scheduler, "SessionLocal", _must_not_open_session)
    scheduler.check_stuck_schedule_adjustments()


@pytest.mark.anyio
@pytest.mark.parametrize("plan_was_paused", [False, True])
async def test_force_reset_clears_context_without_writing_legacy_state(
    monkeypatch,
    plan_was_paused,
):
    user = SimpleNamespace(id=2, tg_id=200, current_state="SCHEDULE_ADJUSTMENT")
    memory = _DummySessionMemory(context={"plan_was_paused": plan_was_paused})
    monkeypatch.setitem(
        sys.modules,
        "app.session_memory",
        SimpleNamespace(session_memory=memory),
    )

    class _DB:
        def add(self, _obj):
            raise AssertionError("legacy user state must not be persisted")

        def commit(self):
            raise AssertionError("legacy user state must not be committed")

    await scheduler._force_reset_schedule_adjustment(user, _DB())

    assert user.current_state == "SCHEDULE_ADJUSTMENT"
    assert memory.context == {}
    assert memory.last_active is None
    assert memory.prompted is False

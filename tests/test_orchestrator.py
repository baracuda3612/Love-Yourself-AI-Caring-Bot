import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

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
    monkeypatch.setattr(orchestrator, "_auto_complete_plan_if_needed_for_user_id", lambda _user_id: None)


# NOTE: coach integration tests deferred to T5.8
# (coach prompt + tool registration not yet implemented)


class _FakeQuery:
    def __init__(self, steps):
        self._steps = steps

    def join(self, *_args, **_kwargs):
        return self

    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        return self._steps


class _FakeDB:
    def __init__(self, steps):
        self._steps = steps

    def query(self, *_args, **_kwargs):
        return _FakeQuery(self._steps)


class _FakeStep:
    def __init__(self, difficulty):
        self.difficulty = difficulty


class _FakePlan:
    def __init__(self, plan_id=1):
        self.id = plan_id


class _AutoCompleteQuery:
    def __init__(self, plans):
        self._plans = plans

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, n):
        self._plans = self._plans[:n]
        return self

    def all(self):
        return self._plans


class _AutoCompleteDB:
    def __init__(self, plans):
        self._plans = plans
        self.added = []

    def query(self, *_args, **_kwargs):
        return _AutoCompleteQuery(self._plans)

    def add(self, obj):
        self.added.append(obj)


def test_get_avg_difficulty_mixed_enum_values():
    db = _FakeDB([_FakeStep("EASY"), _FakeStep("MEDIUM"), _FakeStep("HARD")])
    plan = _FakePlan()

    result = orchestrator.get_avg_difficulty(db, plan)

    assert result == 2


def test_get_avg_difficulty_empty_steps_returns_default_one():
    db = _FakeDB([])
    plan = _FakePlan()

    result = orchestrator.get_avg_difficulty(db, plan)

    assert result == 1


@pytest.mark.parametrize(
    ("state", "expected_status"),
    [
        ("ACTIVE", "Стан: доставка вправ активна"),
        ("ACTIVE_PAUSED", "Стан: доставка вправ призупинена"),
    ],
)
def test_format_plan_status_uses_runtime_state(state, expected_status):
    result = orchestrator._format_plan_status(
        {
            "state": state,
            "plan_active": True,
            "current_day": 3,
            "days_total": 7,
            "days_remaining": 5,
            "steps_completed": 2,
            "steps_total": 7,
            "completion_rate": 29,
        }
    )

    assert expected_status in result
    assert "День 3 з 7 · залишилось 5" in result


def test_format_plan_status_without_current_sequence():
    result = orchestrator._format_plan_status(
        {"state": "IDLE_PLAN_ABORTED", "plan_active": False}
    )

    assert result == "📋 Активних 7 або 14 днів зараз немає."


@pytest.mark.anyio
async def test_mutation_tool_rejects_missing_stable_call_id(monkeypatch):
    called = []
    monkeypatch.setattr(
        orchestrator,
        "_build_tool_registry",
        lambda: {"pause_plan": lambda *_args, **_kwargs: called.append(True)},
    )

    result = await orchestrator._execute_plan_tool(
        7,
        {"name": "pause_plan", "arguments": {}},
    )

    assert result == "⚠️ Не вдалось виконати дію. Спробуй ще раз."
    assert called == []


def test_auto_complete_marks_plan_completed_and_logs_event_with_metrics_error(monkeypatch):
    user = type("UserStub", (), {})()
    user.id = 77

    latest_plan = type("PlanStub", (), {})()
    latest_plan.id = 9
    latest_plan.status = "active"
    latest_plan.created_at = datetime.now(timezone.utc)
    latest_plan.total_days = 14
    latest_plan.focus = "REST"
    latest_plan.load = "MID"
    latest_plan.duration = "MEDIUM"
    latest_plan.end_date = None

    db = _AutoCompleteDB([latest_plan])
    captured = {}

    def fake_log_user_event(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(orchestrator, "log_user_event", fake_log_user_event)
    monkeypatch.setattr(orchestrator, "get_authoritative_current_plan", lambda _db, _uid: latest_plan)
    monkeypatch.setattr(
        orchestrator,
        "complete_current_plan_if_ready",
        lambda _db, **_kwargs: type(
            "Result", (), {"plan_id": 9, "duplicate": False}
        )(),
    )
    def raise_no_loop():
        raise RuntimeError("no running loop")

    monkeypatch.setattr(orchestrator.asyncio, "get_running_loop", raise_no_loop)

    completed_plan_id = orchestrator._auto_complete_plan_if_needed(db, user)

    assert completed_plan_id == 9
    assert captured["event_type"] == "plan_completed"
    assert captured["context"]["plan_id"] == 9
    assert captured["context"]["metrics_error"] is True


def test_auto_complete_without_active_plan_sets_idle_without_logging(monkeypatch):
    user = type("UserStub", (), {})()
    user.id = 88

    db = _AutoCompleteDB([])
    called = {"value": False}

    def fake_log_user_event(**_kwargs):
        called["value"] = True

    monkeypatch.setattr(orchestrator, "log_user_event", fake_log_user_event)
    monkeypatch.setattr(orchestrator, "get_authoritative_current_plan", lambda _db, _uid: None)

    completed_plan_id = orchestrator._auto_complete_plan_if_needed(db, user)

    assert completed_plan_id is None
    assert called["value"] is False


def test_auto_complete_stale_scheduled_plan_is_noop(monkeypatch):
    user = type("UserStub", (), {"id": 88})()
    current_plan = type("PlanStub", (), {"id": 42})()

    monkeypatch.setattr(
        orchestrator,
        "get_authoritative_current_plan",
        lambda _db, _uid: current_plan,
    )

    def fail_completion(*_args, **_kwargs):
        raise AssertionError("stale callback must not reach lifecycle completion")

    monkeypatch.setattr(
        orchestrator,
        "complete_current_plan_if_ready",
        fail_completion,
    )

    completed_plan_id = orchestrator._auto_complete_plan_if_needed(
        object(),
        user,
        expected_plan_id=41,
    )

    assert completed_plan_id is None


def test_auto_complete_does_not_reapply_legacy_mirrors_after_event_failure(monkeypatch):
    user = type("UserStub", (), {})()
    user.id = 101

    latest_plan = type("PlanStub", (), {})()
    latest_plan.id = 22
    latest_plan.status = "active"
    latest_plan.created_at = datetime.now(timezone.utc)
    latest_plan.total_days = 21
    latest_plan.focus = "REST"
    latest_plan.load = "MID"
    latest_plan.duration = "LONG"
    latest_plan.end_date = None

    db = _AutoCompleteDB([latest_plan])
    def fake_log_user_event(**_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(orchestrator, "log_user_event", fake_log_user_event)
    monkeypatch.setattr(orchestrator, "get_authoritative_current_plan", lambda _db, _uid: latest_plan)
    monkeypatch.setattr(
        orchestrator,
        "complete_current_plan_if_ready",
        lambda _db, **_kwargs: type(
            "Result", (), {"plan_id": 22, "duplicate": False}
        )(),
    )

    completed_plan_id = orchestrator._auto_complete_plan_if_needed(db, user)

    assert completed_plan_id == 22
    assert not hasattr(user, "current_state")
    assert not hasattr(user, "plan_end_date")


def test_auto_complete_rejects_multiple_current_plans(monkeypatch):
    user = type("UserStub", (), {})()
    user.id = 99

    latest_plan = type("PlanStub", (), {})()
    latest_plan.id = 10
    latest_plan.status = "active"
    latest_plan.created_at = datetime.now(timezone.utc)
    latest_plan.total_days = 7
    latest_plan.focus = "MIXED"
    latest_plan.load = "LITE"
    latest_plan.duration = "SHORT"
    latest_plan.end_date = None

    older_plan = type("PlanStub", (), {})()
    older_plan.id = 5
    older_plan.status = "active"
    older_plan.created_at = datetime.now(timezone.utc) - timedelta(days=10)
    older_plan.total_days = 7
    older_plan.focus = "REST"
    older_plan.load = "LITE"
    older_plan.duration = "SHORT"
    older_plan.end_date = None

    db = _AutoCompleteDB([latest_plan, older_plan])
    from app.lifecycle import LifecycleInvariantError

    monkeypatch.setattr(
        orchestrator,
        "get_authoritative_current_plan",
        lambda _db, _uid: (_ for _ in ()).throw(
            LifecycleInvariantError("multiple current plans")
        ),
    )

    with pytest.raises(LifecycleInvariantError, match="multiple current plans"):
        orchestrator._auto_complete_plan_if_needed(db, user)


def test_get_avg_difficulty_unknown_value_falls_back_to_one():
    db = _FakeDB([_FakeStep("UNKNOWN"), _FakeStep("HARD")])
    plan = _FakePlan()

    result = orchestrator.get_avg_difficulty(db, plan)

    assert result == 2

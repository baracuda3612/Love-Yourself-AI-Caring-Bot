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
from app.lifecycle import LifecycleEntitlementError, LifecycleResult


class DummyMemory:
    def __init__(self) -> None:
        self.messages = []

    async def append_message(self, user_id, role, text):  # pragma: no cover - helper
        self.messages.append((user_id, role, text))


class PendingActionMemory:
    def __init__(self, pending=None) -> None:
        self.pending = pending
        self.cleared = False

    async def set_pending_action(self, _user_id, value):
        self.pending = value

    async def get_pending_action(self, _user_id):
        return self.pending

    async def clear_pending_action(self, _user_id):
        self.pending = None
        self.cleared = True


@pytest.fixture(autouse=True)
def disable_auto_complete(monkeypatch):
    async def _noop(_user_id):
        return None

    monkeypatch.setattr(orchestrator, "_auto_complete_plan_if_needed_for_user_id", _noop)


# NOTE: coach integration tests deferred to T5.8
# (coach prompt + tool registration not yet implemented)


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


@pytest.mark.anyio
async def test_medium_evening_collection_preserves_activation_source(monkeypatch):
    memory = PendingActionMemory()
    monkeypatch.setattr(orchestrator, "session_memory", memory)
    monkeypatch.setattr(orchestrator, "log_metric", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        orchestrator,
        "_build_tool_registry",
        lambda: {
            "create_followup_plan": lambda *_args, **_kwargs: {
                "status": "needs_evening_time"
            }
        },
    )

    response = await orchestrator._execute_plan_tool(
        7,
        {
            "name": "create_followup_plan",
            "arguments": {"plan_type": "MEDIUM"},
            "call_id": "call-medium-7",
        },
    )

    assert response.startswith("О котрій")
    assert memory.pending == "collect_evening_time_for_medium:call-medium-7"


@pytest.mark.anyio
async def test_medium_cascade_reports_reconciliation_failure_and_keeps_retry_key(
    monkeypatch,
):
    memory = PendingActionMemory(
        "collect_evening_time_for_medium:call-medium-7"
    )
    captured = {}
    monkeypatch.setattr(orchestrator, "session_memory", memory)
    monkeypatch.setattr(orchestrator, "log_metric", lambda *_args, **_kwargs: None)

    def followup(_user_id, args):
        captured.update(args)
        return {
            "status": "error",
            "code": "activation_reconciliation_failed",
            "persisted": True,
            "jobs_reconciled": False,
        }

    monkeypatch.setattr(
        orchestrator,
        "_build_tool_registry",
        lambda: {
            "record_evening_time": lambda *_args, **_kwargs: {"status": "ok"},
            "create_followup_plan": followup,
        },
    )

    response = await orchestrator._execute_plan_tool(
        7,
        {
            "name": "record_evening_time",
            "arguments": {"hhmm": "20:30"},
            "call_id": "call-evening-7",
        },
    )

    assert "План збережено" in response
    assert "розклад" in response
    assert captured["_source_operation_id"] == "call-medium-7"
    assert memory.pending == "collect_evening_time_for_medium:call-medium-7"
    assert memory.cleared is False


@pytest.mark.anyio
async def test_superseded_time_change_does_not_return_success_copy(monkeypatch):
    monkeypatch.setattr(orchestrator, "log_metric", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        orchestrator,
        "_build_tool_registry",
        lambda: {
            "change_day_time": lambda *_args, **_kwargs: {
                "status": "error",
                "code": "superseded",
                "day_time": "16:45",
                "requested_day_time": "15:30",
                "saved": False,
            },
        },
    )

    response = await orchestrator._execute_plan_tool(
        7,
        {
            "name": "change_day_time",
            "arguments": {"hhmm": "15:30"},
            "call_id": "call-time-7",
        },
    )

    assert response == (
        "⚠️ Цей запит на зміну часу вже застарів. Актуальний час: 16:45."
    )


@pytest.mark.anyio
async def test_paused_time_copy_does_not_promise_resume_reconciliation(monkeypatch):
    monkeypatch.setattr(orchestrator, "log_metric", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        orchestrator,
        "_build_tool_registry",
        lambda: {
            "change_evening_time": lambda *_args, **_kwargs: {
                "status": "ok",
                "evening_time": "20:30",
                "saved": True,
                "jobs_reconciled": "deferred",
            },
        },
    )

    response = await orchestrator._execute_plan_tool(
        7,
        {
            "name": "change_evening_time",
            "arguments": {"hhmm": "20:30"},
            "call_id": "call-time-paused",
        },
    )

    assert "під час відновлення" not in response
    assert "зараз не змінено" in response


@pytest.mark.anyio
async def test_superseded_evening_preference_blocks_activation_cascade(monkeypatch):
    memory = PendingActionMemory("collect_evening_time_for_medium:activation-1")
    monkeypatch.setattr(orchestrator, "session_memory", memory)
    monkeypatch.setattr(orchestrator, "log_metric", lambda *_args, **_kwargs: None)
    cascaded = []
    monkeypatch.setattr(
        orchestrator,
        "_build_tool_registry",
        lambda: {
            "record_evening_time": lambda *_args, **_kwargs: {
                "status": "error",
                "code": "superseded",
                "evening_time": "21:15",
                "requested_evening_time": "20:30",
            },
            "create_followup_plan": lambda *_args, **_kwargs: cascaded.append(True),
        },
    )

    response = await orchestrator._execute_plan_tool(
        7,
        {
            "name": "record_evening_time",
            "arguments": {"hhmm": "20:30"},
            "call_id": "evening-old",
        },
    )

    assert "застарів" in response
    assert cascaded == []


@pytest.mark.anyio
async def test_inactive_sender_gets_access_denied_before_coach(monkeypatch):
    memory = DummyMemory()
    coach_called = False

    async def reject_completion(_user_id):
        raise LifecycleEntitlementError("user_not_entitled")

    async def fail_coach(_payload):
        nonlocal coach_called
        coach_called = True
        return {"reply_text": "should not run"}

    monkeypatch.setattr(orchestrator, "session_memory", memory)
    monkeypatch.setattr(
        orchestrator,
        "_auto_complete_plan_if_needed_for_user_id",
        reject_completion,
    )
    monkeypatch.setattr(orchestrator, "coach_agent", fail_coach)

    result = await orchestrator.handle_incoming_message(17, "Привіт")

    assert result == {"reply_text": "Доступ до Love Yourself зараз неактивний."}
    assert coach_called is False
    assert memory.messages == [
        (17, "user", "Привіт"),
        (17, "assistant", "Доступ до Love Yourself зараз неактивний."),
    ]


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
    monkeypatch.setattr(
        orchestrator,
        "require_lifecycle_entitlement",
        lambda _db, _uid: None,
    )
    monkeypatch.setattr(orchestrator, "get_authoritative_current_plan", lambda _db, _uid: latest_plan)
    monkeypatch.setattr(
        orchestrator,
        "complete_current_plan_if_ready",
        lambda _db, **_kwargs: LifecycleResult(
            user_id=77,
            plan_id=9,
            status="completed",
            operation="complete",
        ),
    )

    completion = orchestrator._auto_complete_plan_if_needed(db, user)

    assert completion is not None and completion.plan_id == 9
    assert captured["event_type"] == "plan_completed"
    assert captured["plan_id"] == 9
    assert captured["context"]["metrics_error"] is True


def test_auto_complete_without_active_plan_sets_idle_without_logging(monkeypatch):
    user = type("UserStub", (), {})()
    user.id = 88

    db = _AutoCompleteDB([])
    called = {"value": False}

    def fake_log_user_event(**_kwargs):
        called["value"] = True

    monkeypatch.setattr(orchestrator, "log_user_event", fake_log_user_event)
    monkeypatch.setattr(
        orchestrator,
        "require_lifecycle_entitlement",
        lambda _db, _uid: None,
    )
    monkeypatch.setattr(orchestrator, "get_authoritative_current_plan", lambda _db, _uid: None)

    completed_plan_id = orchestrator._auto_complete_plan_if_needed(db, user)

    assert completed_plan_id is None
    assert called["value"] is False


def test_auto_complete_without_plan_still_enforces_entitlement(monkeypatch):
    user = type("UserStub", (), {"id": 89})()

    def reject_inactive(_db, _user_id):
        raise LifecycleEntitlementError("user_not_entitled")

    monkeypatch.setattr(
        orchestrator,
        "require_lifecycle_entitlement",
        reject_inactive,
    )
    monkeypatch.setattr(
        orchestrator,
        "get_authoritative_current_plan",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must authorize first")),
    )

    with pytest.raises(LifecycleEntitlementError, match="user_not_entitled"):
        orchestrator._auto_complete_plan_if_needed(object(), user)


def test_auto_complete_stale_scheduled_plan_is_noop(monkeypatch):
    user = type("UserStub", (), {"id": 88})()
    current_plan = type("PlanStub", (), {"id": 42})()

    monkeypatch.setattr(
        orchestrator,
        "require_lifecycle_entitlement",
        lambda _db, _uid: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "get_authoritative_current_plan",
        lambda _db, _uid: current_plan,
    )

    called = []

    def fail_completion(*_args, **kwargs):
        called.append(kwargs["plan_id"])
        return None

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
    assert called == [41]


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
    monkeypatch.setattr(
        orchestrator,
        "require_lifecycle_entitlement",
        lambda _db, _uid: None,
    )
    monkeypatch.setattr(orchestrator, "get_authoritative_current_plan", lambda _db, _uid: latest_plan)
    monkeypatch.setattr(
        orchestrator,
        "complete_current_plan_if_ready",
        lambda _db, **_kwargs: LifecycleResult(
            user_id=101,
            plan_id=22,
            status="completed",
            operation="complete",
        ),
    )

    completion = orchestrator._auto_complete_plan_if_needed(db, user)

    assert completion is not None and completion.plan_id == 22
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
        "require_lifecycle_entitlement",
        lambda _db, _uid: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "get_authoritative_current_plan",
        lambda _db, _uid: (_ for _ in ()).throw(
            LifecycleInvariantError("multiple current plans")
        ),
    )

    with pytest.raises(LifecycleInvariantError, match="multiple current plans"):
        orchestrator._auto_complete_plan_if_needed(db, user)

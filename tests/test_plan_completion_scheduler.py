import os
import pathlib
import sys
from datetime import datetime

import pytest
import pytz

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://test-user:test-pass@localhost:5432/test-db",
)
os.environ.setdefault("OPENAI_API_KEY", "test-key")

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

from app import orchestrator, scheduler


class _SessionCtx:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeNowDateTime:
    current = None

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls.current
        return cls.current.astimezone(tz)


class _RecordingActiveUsersQuery:
    def __init__(self):
        self.joins = []
        self.filters = []

    def join(self, *args):
        self.joins.append(args)
        return self

    def filter(self, *criteria):
        self.filters.extend(criteria)
        return self


class _RecordingActiveUsersDB:
    def __init__(self):
        self.user_query = _RecordingActiveUsersQuery()

    def query(self, model):
        assert model is scheduler.User
        return self.user_query


def test_scheduler_user_scan_requires_an_authoritative_active_plan():
    db = _RecordingActiveUsersDB()

    query = scheduler._active_plan_users_query(db)

    assert query is db.user_query
    assert db.user_query.joins
    assert db.user_query.joins[0][0] is scheduler.AIPlan
    assert any(
        criterion.compare(scheduler.AIPlan.status == "active")
        for criterion in db.user_query.filters
    )


class _FakeStepQuery:
    def __init__(self, count_value):
        self._count = count_value

    def join(self, *_args, **_kwargs):
        return self

    def filter(self, *_args, **_kwargs):
        return self

    def count(self):
        return self._count


class _FakeUserQuery:
    def __init__(self, user):
        self._user = user

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._user


class _DBForMaybeSchedule:
    def __init__(self, future_steps, user):
        self.future_steps = future_steps
        self.user = user

    def query(self, model):
        if model is scheduler.AIPlanStep:
            return _FakeStepQuery(self.future_steps)
        if model is scheduler.User:
            return _FakeUserQuery(self.user)
        raise AssertionError("unexpected model")


def test_maybe_schedule_plan_completion_schedules_on_last_step(monkeypatch):
    user = type("U", (), {"id": 1, "timezone": "Europe/Kyiv"})()
    fake_db = _DBForMaybeSchedule(future_steps=0, user=user)
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: _SessionCtx(fake_db))

    calls = []
    monkeypatch.setattr(scheduler.scheduler, "add_job", lambda *a, **k: calls.append((a, k)))

    _FakeNowDateTime.current = datetime(2026, 1, 1, 16, 0, tzinfo=pytz.UTC)
    monkeypatch.setattr(scheduler, "datetime", _FakeNowDateTime)

    scheduler._maybe_schedule_plan_completion(user_id=1, plan_id=10)

    assert len(calls) == 1
    assert calls[0][1]["id"] == "completion_10"


def test_maybe_schedule_plan_completion_skips_when_future_steps(monkeypatch):
    user = type("U", (), {"id": 1, "timezone": "Europe/Kyiv"})()
    fake_db = _DBForMaybeSchedule(future_steps=2, user=user)
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: _SessionCtx(fake_db))

    calls = []
    monkeypatch.setattr(scheduler.scheduler, "add_job", lambda *a, **k: calls.append((a, k)))

    _FakeNowDateTime.current = datetime(2026, 1, 1, 16, 0, tzinfo=pytz.UTC)
    monkeypatch.setattr(scheduler, "datetime", _FakeNowDateTime)

    scheduler._maybe_schedule_plan_completion(user_id=1, plan_id=10)

    assert calls == []


def test_maybe_schedule_plan_completion_run_date_plus_two_hours(monkeypatch):
    user = type("U", (), {"id": 1, "timezone": "Europe/Kyiv"})()
    fake_db = _DBForMaybeSchedule(future_steps=0, user=user)
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: _SessionCtx(fake_db))

    calls = []
    monkeypatch.setattr(scheduler.scheduler, "add_job", lambda *a, **k: calls.append((a, k)))

    _FakeNowDateTime.current = datetime(2026, 1, 1, 15, 0, tzinfo=pytz.UTC)  # 17:00 local
    monkeypatch.setattr(scheduler, "datetime", _FakeNowDateTime)

    scheduler._maybe_schedule_plan_completion(user_id=1, plan_id=10)

    run_date = calls[0][1]["run_date"]
    assert run_date == datetime(2026, 1, 1, 17, 0, tzinfo=pytz.UTC)


def test_maybe_schedule_plan_completion_run_date_next_day_10_local(monkeypatch):
    user = type("U", (), {"id": 1, "timezone": "Europe/Kyiv"})()
    fake_db = _DBForMaybeSchedule(future_steps=0, user=user)
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: _SessionCtx(fake_db))

    calls = []
    monkeypatch.setattr(scheduler.scheduler, "add_job", lambda *a, **k: calls.append((a, k)))

    _FakeNowDateTime.current = datetime(2026, 1, 1, 19, 30, tzinfo=pytz.UTC)  # 21:30 local
    monkeypatch.setattr(scheduler, "datetime", _FakeNowDateTime)

    scheduler._maybe_schedule_plan_completion(user_id=1, plan_id=10)

    run_date = calls[0][1]["run_date"]
    assert run_date == datetime(2026, 1, 2, 8, 0, tzinfo=pytz.UTC)


def test_maybe_schedule_plan_completion_moves_to_next_day_if_candidate_after_21(monkeypatch):
    user = type("U", (), {"id": 1, "timezone": "Europe/Kyiv"})()
    fake_db = _DBForMaybeSchedule(future_steps=0, user=user)
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: _SessionCtx(fake_db))

    calls = []
    monkeypatch.setattr(scheduler.scheduler, "add_job", lambda *a, **k: calls.append((a, k)))

    _FakeNowDateTime.current = datetime(2026, 1, 1, 18, 30, tzinfo=pytz.UTC)  # 20:30 local; +2h => 22:30 local
    monkeypatch.setattr(scheduler, "datetime", _FakeNowDateTime)

    scheduler._maybe_schedule_plan_completion(user_id=1, plan_id=10)

    run_date = calls[0][1]["run_date"]
    assert run_date == datetime(2026, 1, 2, 8, 0, tzinfo=pytz.UTC)


class _FakeEventQuery:
    def __init__(self, existing):
        self._existing = existing

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._existing


class _DBForCompletionMessage:
    def __init__(self, user, existing_event):
        self.user = user
        self.existing_event = existing_event
        self.commits = 0

    def query(self, model):
        if model is orchestrator.User:
            return _FakeUserQuery(self.user)
        if model is orchestrator.UserEvent:
            return _FakeEventQuery(self.existing_event)
        raise AssertionError("unexpected model")

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


class _SubmittedFuture:
    def result(self, timeout=None):
        return None


def test_trigger_plan_completion_skips_report_when_plan_is_not_ready(monkeypatch):
    user = type("U", (), {"id": 1})()
    db = _DBForCompletionMessage(user=user, existing_event=None)
    monkeypatch.setattr(orchestrator, "SessionLocal", lambda: _SessionCtx(db))
    monkeypatch.setattr(
        orchestrator,
        "_auto_complete_plan_if_needed",
        lambda _db, _user: None,
    )

    submitted = []
    monkeypatch.setattr(
        "app.scheduler._submit_coroutine",
        lambda coro: submitted.append(coro),
    )

    orchestrator._trigger_plan_completion(user_id=1, plan_id=99)

    assert db.commits == 1
    assert submitted == []


def test_trigger_plan_completion_reports_the_plan_that_completed(monkeypatch):
    user = type("U", (), {"id": 1})()
    db = _DBForCompletionMessage(user=user, existing_event=None)
    monkeypatch.setattr(orchestrator, "SessionLocal", lambda: _SessionCtx(db))
    monkeypatch.setattr(
        orchestrator,
        "_auto_complete_plan_if_needed",
        lambda _db, _user: 42,
    )

    submitted = []

    async def _fake_send(user_id, plan_id):
        submitted.append((user_id, plan_id))

    def _fake_submit(coro):
        try:
            coro.send(None)
        except StopIteration:
            pass
        return _SubmittedFuture()

    monkeypatch.setattr(orchestrator, "send_plan_completion_message", _fake_send)
    monkeypatch.setattr("app.scheduler._submit_coroutine", _fake_submit)

    orchestrator._trigger_plan_completion(user_id=1, plan_id=99)

    assert db.commits == 1
    assert submitted == [(1, 42)]


@pytest.mark.anyio
async def test_message_entry_completion_wrapper_captures_result_before_send(monkeypatch):
    user = type("U", (), {"id": 1})()
    db = _DBForCompletionMessage(user=user, existing_event=None)
    monkeypatch.setattr(orchestrator, "SessionLocal", lambda: _SessionCtx(db))
    monkeypatch.setattr(
        orchestrator,
        "_auto_complete_plan_if_needed",
        lambda _db, _user: 42,
    )

    sent = []

    async def _fake_send(user_id, plan_id):
        sent.append((user_id, plan_id))

    created = []

    def _fake_create_task(coro):
        created.append(coro)
        return None

    monkeypatch.setattr(orchestrator, "send_plan_completion_message", _fake_send)
    monkeypatch.setattr(orchestrator.asyncio, "create_task", _fake_create_task)

    orchestrator._auto_complete_plan_if_needed_for_user_id(1)

    assert db.commits == 1
    assert len(created) == 1
    await created[0]
    assert sent == [(1, 42)]


@pytest.mark.anyio
async def test_send_plan_completion_message_skips_when_already_sent(monkeypatch):
    user = type("U", (), {"id": 1, "tg_id": 123, "profile": None})()
    db = _DBForCompletionMessage(user=user, existing_event=object())
    monkeypatch.setattr(orchestrator, "SessionLocal", lambda: _SessionCtx(db))

    sent = []
    monkeypatch.setattr(orchestrator, "log_user_event", lambda *a, **k: sent.append((a, k)))

    async def _fake_send(*_args, **_kwargs):
        sent.append("sent")
        return True

    monkeypatch.setattr("app.scheduler._send_message_async", _fake_send)

    await orchestrator.send_plan_completion_message(1, 99)

    assert sent == []


@pytest.mark.anyio
async def test_send_plan_completion_message_skips_when_no_tg_id(monkeypatch):
    user = type("U", (), {"id": 1, "tg_id": None, "profile": None})()
    db = _DBForCompletionMessage(user=user, existing_event=None)
    monkeypatch.setattr(orchestrator, "SessionLocal", lambda: _SessionCtx(db))

    async def _fake_send(*_args, **_kwargs):
        raise AssertionError("should not send")

    monkeypatch.setattr("app.scheduler._send_message_async", _fake_send)

    await orchestrator.send_plan_completion_message(1, 99)


class _ExpiredUserQuery:
    def __init__(self, users):
        self.users = users

    def join(self, *_args, **_kwargs):
        return self

    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        return self.users


class _EmptyPlanQuery:
    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def first(self):
        return None

    def all(self):
        return []


class _DBForCron:
    def __init__(self, users):
        self.users = users
        self.committed = False

    def query(self, model):
        if model is scheduler.User:
            return _ExpiredUserQuery(self.users)
        if model is scheduler.AIPlan:
            return _EmptyPlanQuery()
        raise AssertionError("unexpected model")

    def commit(self):
        self.committed = True

    def rollback(self):
        self.committed = False


def test_check_plan_completions_calls_auto_complete(monkeypatch):
    plans = [
        type("P", (), {"id": 10, "user_id": 1})(),
        type("P", (), {"id": 20, "user_id": 2})(),
    ]

    class _PlansQuery:
        def filter(self, *_args, **_kwargs):
            return self

        def all(self):
            return plans

    db = _DBForCron([])
    db.query = lambda _model: _PlansQuery()
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: _SessionCtx(db))

    calls = []

    def _fake_complete(_db, **kwargs):
        calls.append(kwargs["user_id"])
        return type("R", (), {"status": "completed"})()

    monkeypatch.setattr(scheduler, "complete_current_plan_if_ready", _fake_complete)
    monkeypatch.setattr(scheduler, "_event_loop", None)

    scheduler.check_plan_completions()

    assert calls == [1, 2]
    assert db.committed is True



def test_check_plan_completions_submits_completion_messages_when_event_loop_available(monkeypatch):
    users = [type("U", (), {"id": 1})()]

    class _Plan:
        def __init__(self, plan_id):
            self.id = plan_id
            self.user_id = 1
            self.status = "active"

    class _PlanQuery:
        def __init__(self, plan):
            self.plan = plan

        def filter(self, *_args, **_kwargs):
            return self

        def order_by(self, *_args, **_kwargs):
            return self

        def first(self):
            return self.plan

        def all(self):
            return [self.plan]

    class _DBForCronWithPlans(_DBForCron):
        def __init__(self, users, plan):
            super().__init__(users)
            self.plan = plan

        def query(self, model):
            if model is scheduler.User:
                return _ExpiredUserQuery(self.users)
            if model is scheduler.AIPlan:
                return _PlanQuery(self.plan)
            raise AssertionError("unexpected model")

    plan = _Plan(plan_id=42)
    db = _DBForCronWithPlans(users, plan)
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: _SessionCtx(db))

    def _fake_complete(_db, **_kwargs):
        return type("R", (), {"status": "completed"})()

    monkeypatch.setattr(scheduler, "complete_current_plan_if_ready", _fake_complete)

    submitted = []

    class _Future:
        def result(self, timeout=None):
            return None

    monkeypatch.setattr(scheduler, "_event_loop", object())
    def _fake_submit(coro):
        submitted.append(coro)
        coro.close()
        return _Future()

    monkeypatch.setattr(scheduler, "_submit_coroutine", _fake_submit)

    scheduler.check_plan_completions()

    assert len(submitted) == 1

import os
from datetime import datetime, timezone

import pytest

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://test-user:test-pass@localhost:5432/test-db",
)
os.environ.setdefault("OPENAI_API_KEY", "test-key")

from app import telegram


class DummyUser:
    def __init__(self, tg_id: int, user_id: int) -> None:
        self.tg_id = tg_id
        self.id = user_id


class DummyPlan:
    def __init__(self, user: DummyUser) -> None:
        self.user = user
        self.user_id = user.id


class DummyDay:
    def __init__(self, plan: DummyPlan, day_number: int = 1) -> None:
        self.plan = plan
        self.day_number = day_number


class DummyStep:
    def __init__(
        self,
        step_id: int,
        day: DummyDay,
        exercise_id: str = "exercise-1",
        is_completed: bool = False,
        skipped: bool = False,
        completed_at: datetime | None = None,
    ) -> None:
        self.id = step_id
        self.day = day
        self.exercise_id = exercise_id
        self.is_completed = is_completed
        self.skipped = skipped
        self.completed_at = completed_at


class DummyMessage:
    def __init__(self) -> None:
        self.edited_reply_markup = None

    async def edit_reply_markup(self, reply_markup=None):
        self.edited_reply_markup = reply_markup


class DummyFromUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class DummyCallbackQuery:
    def __init__(self, data: str, user_id: int, message: DummyMessage | None = None) -> None:
        self.data = data
        self.from_user = DummyFromUser(user_id)
        self.message = message
        self.answers = []

    async def answer(self, text: str | None = None):
        self.answers.append(text)


class FakeQuery:
    def __init__(self, step: DummyStep | None) -> None:
        self.step = step

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self.step


class FakeSession:
    def __init__(self, step: DummyStep | None) -> None:
        self.step = step
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def query(self, _model):
        return FakeQuery(self.step)

    def commit(self):
        self.committed = True


@pytest.mark.anyio
async def test_task_completed_happy_path(monkeypatch):
    user = DummyUser(tg_id=123, user_id=42)
    plan = DummyPlan(user=user)
    day = DummyDay(plan=plan, day_number=3)
    step = DummyStep(step_id=101, day=day)
    message = DummyMessage()
    callback_query = DummyCallbackQuery(
        data="task_complete:101",
        user_id=123,
        message=message,
    )

    fake_session = FakeSession(step)
    monkeypatch.setattr(telegram, "SessionLocal", lambda: fake_session)

    logged_events = []

    def fake_log_user_event(db, user_id, event_type, plan_step_id=None, context=None, **_kwargs):
        logged_events.append(
            {
                "db": db,
                "user_id": user_id,
                "event_type": event_type,
                "plan_step_id": plan_step_id,
                "context": context,
            }
        )

    monkeypatch.setattr(telegram, "log_user_event", fake_log_user_event)

    await telegram.handle_task_completed(callback_query)

    assert step.is_completed is True
    assert step.skipped is False
    assert step.completed_at is not None
    assert step.completed_at.tzinfo == timezone.utc
    assert fake_session.committed is True
    assert logged_events == [
        {
            "db": fake_session,
            "user_id": 42,
            "event_type": "task_completed",
            "plan_step_id": 101,
            "context": {"exercise_id": "exercise-1", "day_number": 3},
        }
    ]
    assert callback_query.answers[-1] == "✅ Чудово! Завдання виконано."
    assert message.edited_reply_markup is None


@pytest.mark.anyio
async def test_task_skipped_happy_path(monkeypatch):
    user = DummyUser(tg_id=321, user_id=24)
    plan = DummyPlan(user=user)
    day = DummyDay(plan=plan, day_number=2)
    step = DummyStep(step_id=202, day=day)
    message = DummyMessage()
    callback_query = DummyCallbackQuery(
        data="task_skip:202",
        user_id=321,
        message=message,
    )

    fake_session = FakeSession(step)
    monkeypatch.setattr(telegram, "SessionLocal", lambda: fake_session)

    logged_events = []

    def fake_log_user_event(db, user_id, event_type, plan_step_id=None, context=None, **_kwargs):
        logged_events.append(
            {
                "db": db,
                "user_id": user_id,
                "event_type": event_type,
                "plan_step_id": plan_step_id,
                "context": context,
            }
        )

    monkeypatch.setattr(telegram, "log_user_event", fake_log_user_event)

    await telegram.handle_task_skipped(callback_query)

    assert step.skipped is True
    assert step.is_completed is False
    assert fake_session.committed is True
    assert logged_events == [
        {
            "db": fake_session,
            "user_id": 24,
            "event_type": "task_skipped",
            "plan_step_id": 202,
            "context": {"exercise_id": "exercise-1", "day_number": 2},
        }
    ]
    assert callback_query.answers[-1] == "⏭️ Завдання пропущено"
    assert message.edited_reply_markup is None


@pytest.mark.anyio
async def test_cannot_complete_others_task(monkeypatch):
    owner = DummyUser(tg_id=111, user_id=1)
    other_user_id = 222
    plan = DummyPlan(user=owner)
    day = DummyDay(plan=plan, day_number=1)
    step = DummyStep(step_id=303, day=day)
    callback_query = DummyCallbackQuery(
        data="task_complete:303",
        user_id=other_user_id,
        message=DummyMessage(),
    )

    fake_session = FakeSession(step)
    monkeypatch.setattr(telegram, "SessionLocal", lambda: fake_session)

    logged_events = []

    def fake_log_user_event(*_args, **_kwargs):
        logged_events.append("logged")

    monkeypatch.setattr(telegram, "log_user_event", fake_log_user_event)

    await telegram.handle_task_completed(callback_query)

    assert callback_query.answers[-1] == "Це не ваше завдання"
    assert fake_session.committed is False
    assert logged_events == []


@pytest.mark.anyio
async def test_already_completed(monkeypatch):
    user = DummyUser(tg_id=555, user_id=5)
    plan = DummyPlan(user=user)
    day = DummyDay(plan=plan, day_number=1)
    step = DummyStep(step_id=404, day=day, is_completed=True)
    callback_query = DummyCallbackQuery(
        data="task_complete:404",
        user_id=555,
        message=DummyMessage(),
    )

    fake_session = FakeSession(step)
    monkeypatch.setattr(telegram, "SessionLocal", lambda: fake_session)

    logged_events = []

    def fake_log_user_event(*_args, **_kwargs):
        logged_events.append("logged")

    monkeypatch.setattr(telegram, "log_user_event", fake_log_user_event)

    await telegram.handle_task_completed(callback_query)

    assert callback_query.answers[-1] == "Вже виконано"
    assert fake_session.committed is False
    assert logged_events == []

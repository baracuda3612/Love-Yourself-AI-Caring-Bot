"""Coach keeps the last delivered exercise after terminal keyboard cleanup."""

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app import telegram
from app.content_library import DEFAULT_SEED_PATH, FIELDS
from app.db import (
    AIPlan, AIPlanDay, AIPlanStep, Base, ContentLibrary,
    PlanLifecycleOperation, User,
)
from app.exercise_presentation import current_exercise_context


@compiles(JSONB, 'sqlite')
def _sqlite_jsonb(_type, _compiler, **_kwargs):
    # The real PostgreSQL path has separate disposable-DB acceptance tests.
    return JSON().compile(dialect=_compiler.dialect)


@pytest.fixture
def coach_db():
    engine = create_engine('sqlite:///:memory:')

    @event.listens_for(engine, 'connect')
    def _register_btrim(connection, _record):
        connection.create_function('btrim', 1, lambda value: value.strip())

    Base.metadata.create_all(
        engine, tables=[
            User.__table__, ContentLibrary.__table__, AIPlan.__table__,
            AIPlanDay.__table__, AIPlanStep.__table__, PlanLifecycleOperation.__table__,
        ],
    )
    with Session(engine) as db:
        records = json.loads(DEFAULT_SEED_PATH.read_text())['inventory'][:2]
        for record in records:
            db.add(ContentLibrary(
                exercise_id=record['id'],
                **{field: record[field] for field in FIELDS},
            ))
        db.add(User(id=1, tg_id=101, timezone='Europe/Kyiv'))
        db.add(AIPlan(id=1, user_id=1, title='Test', cycle_number=1,
                      total_days=7, activated_at=datetime.now(timezone.utc),
                      status='active'))
        db.add(AIPlanDay(id=1, plan_id=1, day_number=1))
        db.flush()
        now = datetime.now(timezone.utc)
        for index, record in enumerate(records, 1):
            db.add(AIPlanStep(
                id=index, day_id=1, order_in_day=index,
                exercise_id=record['id'], content_version=1,
                content_snapshot=record, title=record['display']['title'],
                description='\n'.join(record['display']['steps']),
                step_status='delivered', tg_message_id=70 + index,
                scheduled_for=now - timedelta(minutes=5),
                expires_at=now + timedelta(hours=1),
            ))
        db.flush()
        yield db, records
    engine.dispose()


@contextmanager
def _retained_session(db):
    yield db


@pytest.mark.parametrize('terminal', ['completed', 'skipped'])
async def test_latest_delivered_exercise_survives_actual_keyboard_cleanup(
    coach_db, monkeypatch, terminal,
):
    db, records = coach_db
    older, latest = (db.get(AIPlanStep, step_id) for step_id in (1, 2))

    # A terminal step and a Telegram marker alone are not proof of delivery.
    latest.step_status = terminal
    latest.terminal_at = datetime.now(timezone.utc)
    db.flush()
    assert current_exercise_context(db, 1, 1) is None

    older_receipt = PlanLifecycleOperation(
        id=1, user_id=1, plan_id=1, plan_step_id=older.id,
        source_operation_id='test:older-delivery', operation='step_delivered',
        result_status='delivered', created_at=datetime.now(timezone.utc) - timedelta(minutes=2),
    )
    latest_receipt = PlanLifecycleOperation(
        id=2, user_id=1, plan_id=1, plan_step_id=latest.id,
        source_operation_id='test:latest-delivery', operation='step_delivered',
        result_status='delivered', created_at=datetime.now(timezone.utc),
    )
    db.add_all([older_receipt, latest_receipt])
    db.commit()
    assert current_exercise_context(db, 1, 1)['exercise_id'] == records[1]['id']

    monkeypatch.setattr(telegram, 'SessionLocal', lambda: _retained_session(db))
    message = SimpleNamespace(message_id=latest.tg_message_id,
                              edit_reply_markup=AsyncMock())
    callback = SimpleNamespace(message=message)
    assert await telegram._clear_terminal_callback_keyboard(callback, latest.id)
    message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
    db.refresh(latest)
    assert latest.tg_message_id is None

    context = current_exercise_context(db, 1, 1)
    assert context['exercise_id'] == records[1]['id']
    assert context['steps'] == records[1]['display']['steps']
    assert context['status'] == terminal
    assert context['available_actions'] == []
    assert current_exercise_context(db, 2, 1) is None
    assert current_exercise_context(db, 1, 2) is None

    # Content gates still apply even to a receipt-proven historical send.
    content = db.get(ContentLibrary, (latest.exercise_id, latest.content_version))
    content.is_active = False
    db.flush()
    assert current_exercise_context(db, 1, 1) is None

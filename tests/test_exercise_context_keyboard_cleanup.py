"""Coach keeps the last delivered exercise after terminal keyboard cleanup."""

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import JSON, create_engine, event, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import telegram
from app.content_library import DEFAULT_SEED_PATH, FIELDS
from app.db import (
    AIPlan, AIPlanDay, AIPlanStep, Base, ContentLibrary,
    OnboardingProgress, PlanCompletion, PlanContinuationAttempt, PlanLifecycleOperation, User, UserProfile,
)
from app.exercise_presentation import current_exercise_context


@compiles(JSONB, 'sqlite')
def _sqlite_jsonb(_type, _compiler, **_kwargs):
    # The real PostgreSQL path has separate disposable-DB acceptance tests.
    return JSON().compile(dialect=_compiler.dialect)


@pytest.fixture
def coach_db():
    engine = create_engine(
        'sqlite:///:memory:', poolclass=StaticPool, connect_args={'check_same_thread': False},
    )

    @event.listens_for(engine, 'connect')
    def _register_btrim(connection, _record):
        connection.create_function('btrim', 1, lambda value: value.strip())

    Base.metadata.create_all(
        engine, tables=[
            User.__table__, ContentLibrary.__table__, AIPlan.__table__,
            AIPlanDay.__table__, AIPlanStep.__table__, PlanLifecycleOperation.__table__,
            UserProfile.__table__, OnboardingProgress.__table__, PlanCompletion.__table__, PlanContinuationAttempt.__table__,
        ],
    )
    with Session(engine) as db:
        db.execute(text('DROP INDEX ux_ai_plans_one_current_per_user'))
        db.execute(text("CREATE UNIQUE INDEX ux_ai_plans_one_current_per_user "
                        "ON ai_plans (user_id) WHERE status IN ('active', 'paused')"))
        # SQLite fixture for the read path. PostgreSQL owns the actual journal DDL.
        db.execute(text('CREATE TABLE exercise_deliveries '
                        '(plan_step_id INTEGER, confirmed_at DATETIME, state TEXT, message_id INTEGER)'))
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

    # A fixed instant avoids a flaky fixture around the real user's midnight.
    now = datetime(2026, 11, 7, 13, tzinfo=timezone.utc)
    assert current_exercise_context(db, 1, 1, now=now) is None
    older_receipt = PlanLifecycleOperation(
        id=1, user_id=1, plan_id=1, plan_step_id=older.id,
        source_operation_id='test:older-delivery', operation='step_delivered',
        result_status='delivered', created_at=now - timedelta(minutes=2),
    )
    latest_receipt = PlanLifecycleOperation(
        id=2, user_id=1, plan_id=1, plan_step_id=latest.id,
        source_operation_id='test:latest-delivery', operation='step_delivered',
        result_status='delivered', created_at=now,
    )
    db.add_all([older_receipt, latest_receipt])
    db.commit()
    assert current_exercise_context(db, 1, 1, now=now)['exercise_id'] == records[1]['id']

    monkeypatch.setattr(telegram, 'SessionLocal', lambda: _retained_session(db))
    message = SimpleNamespace(message_id=latest.tg_message_id,
                              edit_reply_markup=AsyncMock())
    callback = SimpleNamespace(message=message)
    assert await telegram._clear_terminal_callback_keyboard(callback, latest.id)
    message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
    db.refresh(latest)
    assert latest.tg_message_id is None

    context = current_exercise_context(db, 1, 1, now=now)
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
    assert current_exercise_context(db, 1, 1, now=now) is None


@pytest.mark.parametrize(('timestamps', 'expected_index'), [
    ((), None),
    (((1, 21, 59),), None),
    (((1, 22, 1),), 0),
    (((1, 22, 5), (2, 22, 25)), 1),
    (((1, 22, 25), (2, 22, 5)), 0),
    (((1, 22, 25), (2, 22, 25)), 1),
])
def test_coach_uses_delivery_day_and_send_order(coach_db, timestamps, expected_index):
    db, records = coach_db
    tz = ZoneInfo('Europe/Kyiv')
    now = datetime(2026, 11, 7, 23, 30, tzinfo=timezone.utc)
    for step_id, hour, minute in timestamps:
        actual = now.replace(hour=hour, minute=minute)
        db.add(PlanLifecycleOperation(
            id=3 - step_id, user_id=1, plan_id=1, plan_step_id=step_id,
            source_operation_id=f'delivery:{step_id}', operation='step_delivered',
            result_status='delivered', created_at=now - timedelta(minutes=step_id),
        ))
        db.execute(text(
            "INSERT INTO exercise_deliveries VALUES (:step_id, :sent, 'delivered', :message_id)"
        ), {
            'step_id': step_id, 'sent': actual.replace(tzinfo=None).isoformat(sep=' '),
            'message_id': 70 + step_id,
        })
    db.flush()
    result = current_exercise_context(db, 1, 1, user_timezone=tz, now=now)
    if expected_index is None:
        assert result is None
    else:
        expected = records[expected_index]
        assert result['exercise_id'] == expected['id']
        assert result['steps'] == expected['display']['steps']
        assert result['duration_label'] == expected['display']['duration_label']


def test_latest_delivered_exercise_survives_automatic_cycle_change(coach_db):
    db, records = coach_db
    now = datetime(2026, 11, 7, 21, 55, tzinfo=timezone.utc)
    tz = ZoneInfo('Europe/Kyiv')
    plan = db.get(AIPlan, 1)
    plan.status = 'completed'
    step = db.get(AIPlanStep, 2)
    step.step_status = 'completed'
    step.terminal_at = now
    db.flush()
    db.add(AIPlan(
        id=2, user_id=1, title='Successor', cycle_number=2, total_days=7,
        status='active', activated_at=now,
    ))
    db.add(PlanLifecycleOperation(
        id=1, user_id=1, plan_id=1, plan_step_id=2,
        source_operation_id='test:last-delivery', operation='step_delivered',
        result_status='delivered', created_at=now - timedelta(minutes=3),
    ))
    db.flush()
    assert current_exercise_context(db, 1, 2, user_timezone=tz, now=now) is None

    db.add(PlanCompletion(
        plan_id=1, successor_plan_id=2, state='failed', report_at=now, wake_at=now,
    ))
    db.flush()
    context = current_exercise_context(db, 1, 2, user_timezone=tz, now=now)
    assert context['exercise_id'] == records[1]['id']
    assert context['from_completed_cycle'] is True
    assert context['available_actions'] == []
    assert current_exercise_context(db, 2, 2, user_timezone=tz, now=now) is None
    assert current_exercise_context(db, 1, 2, user_timezone=tz, now=now + timedelta(minutes=10)) is None

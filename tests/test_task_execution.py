"""Transport contract; authoritative state/concurrency is covered by WP-03.4 PG tests."""
import asyncio
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app import telegram
from app.lifecycle import LifecycleResult, LifecycleOwnershipError, LifecycleEntitlementError, LifecycleTransitionError
from app.step_action_ingress import has_timely_step_action


def callback(data='task_complete:10'):
    return SimpleNamespace(id='update-1', data=data, from_user=SimpleNamespace(id=123),
                           message=SimpleNamespace(answer=AsyncMock()), answer=AsyncMock())


@pytest.mark.parametrize('target,status', [('completed','completed'), ('skipped','skipped'), ('completed','expired'), ('skipped','canceled'), ('skipped','completed')])
@pytest.mark.parametrize('duplicate', [False, True])
async def test_action_reports_winning_state_and_projects_same_message(monkeypatch, target, status, duplicate):
    cb = callback('task_complete:10' if target == 'completed' else 'task_skip:10')
    db = SimpleNamespace(commit=Mock())
    boundary = Mock(return_value=LifecycleResult(user_id=1, plan_id=2, step_id=10, status=status, operation='step_'+target, duplicate=duplicate))
    project = AsyncMock()
    monkeypatch.setattr(telegram, 'SessionLocal', lambda: nullcontext(db))
    monkeypatch.setattr(telegram, 'transition_owned_plan_step', boundary)
    monkeypatch.setattr(telegram, '_project_step_status', project)
    monkeypatch.setattr(telegram, 'log_user_event', Mock(side_effect=AssertionError('event belongs inside lifecycle')))
    await telegram._handle_step_action(cb, target)
    assert boundary.call_args.kwargs['telegram_user_id'] == 123
    assert boundary.call_args.kwargs['telegram_message'] is cb.message
    db.commit.assert_called_once()
    cb.answer.assert_awaited_once_with(telegram._STEP_REPLIES[status])
    project.assert_awaited_once_with(10)
    cb.message.answer.assert_not_awaited()


@pytest.mark.parametrize('error,reply', [(LifecycleOwnershipError('owner'), 'Це не ваше завдання'), (LifecycleEntitlementError('inactive'), 'Дія зараз недоступна'), (LifecycleTransitionError('step_not_delivered'), 'Доставку ще не підтверджено'), (RuntimeError('DB unavailable'), 'Не вдалося зберегти дію. Спробуй ще раз')])
async def test_failed_action_does_not_claim_success_or_edit(monkeypatch, error, reply):
    db = SimpleNamespace(commit=Mock())
    cb = callback()
    monkeypatch.setattr(telegram, 'SessionLocal', lambda: nullcontext(db))
    monkeypatch.setattr(telegram, 'transition_owned_plan_step', Mock(side_effect=error))
    project = AsyncMock(); monkeypatch.setattr(telegram, '_project_step_status', project)
    await telegram.handle_task_completed(cb)
    cb.answer.assert_awaited_once_with(reply)
    project.assert_not_awaited(); db.commit.assert_not_called()


@pytest.mark.parametrize('data', ['task_complete:bad', 'task_skip:', 'task_complete'])
async def test_malformed_action_is_factual(data):
    cb = callback(data)
    await telegram.handle_task_completed(cb)
    cb.answer.assert_awaited_once_with('Завдання не знайдено')


@pytest.mark.parametrize("stop_while_queued", [False, True])
async def test_polling_keeps_timely_actions_in_entire_buffered_batch(monkeypatch, stop_while_queued):
    from aiogram.methods import GetMe, GetUpdates, SetMyCommands
    from aiogram.types import Update, User

    occupied, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    started = {}
    batch = []
    for number in range(1, 35):
        actor_id = 900_000 + number
        batch.append(Update.model_validate({
            "update_id": number,
            "callback_query": {
                "id": str(number), "from": {"id": actor_id, "is_bot": False, "first_name": "Test"},
                "chat_instance": str(actor_id), "data": f"task_complete:{number}",
                "message": {"message_id": 777, "date": 1, "chat": {"id": actor_id, "type": "private"}},
            },
        }, context={"bot": telegram.bot}))

    batch_received_at = []

    async def transport(_bot, method, **_kwargs):
        if isinstance(method, GetMe):
            return User(id=telegram.bot.id, is_bot=True, first_name="Test bot")
        if isinstance(method, SetMyCommands):
            return True
        assert isinstance(method, GetUpdates)
        if batch:
            batch_received_at.append(datetime.now(timezone.utc))
            updates = batch[:]
            batch.clear()
            return updates
        await asyncio.Future()

    async def handle(callback_query, _target, received_at):
        number = int(callback_query.id)
        started[number] = received_at
        if len(started) == 32:
            occupied.set()
        if number <= 32:
            await release.wait()
        elif 33 in started and 34 in started:
            completed.set()

    monkeypatch.setattr(telegram.bot.session, "make_request", transport)
    monkeypatch.setattr(telegram, "run_db", AsyncMock())
    monkeypatch.setattr(telegram, "_handle_step_action", handle)
    monkeypatch.setattr(telegram.dp, "_stop_signal", None)
    monkeypatch.setattr(telegram.dp, "_stopped_signal", None)
    polling = asyncio.create_task(telegram.dp.start_polling(
        telegram.bot, tasks_concurrency_limit=32, handle_signals=False, close_bot_session=False,
    ))
    try:
        await asyncio.wait_for(occupied.wait(), 3)
        assert set(started) == set(range(1, 33))
        deadline = datetime.now(timezone.utc)
        assert has_timely_step_action(900_033, 33, deadline)
        assert has_timely_step_action(900_034, 34, deadline)
        if not stop_while_queued:
            release.set()
            await asyncio.wait_for(completed.wait(), 3)
            assert batch_received_at[0] <= started[33] == started[34] < deadline
    finally:
        if not polling.done():
            await asyncio.wait_for(telegram.dp.stop_polling(), 3)
        release.set()
        await asyncio.wait_for(polling, 3)
        await asyncio.gather(*telegram.dp._handle_update_tasks)

    cutoff = datetime.now(timezone.utc) + timedelta(seconds=1)
    assert not any(has_timely_step_action(900_000 + n, n, cutoff) for n in range(1, 35))
    if stop_while_queued:
        assert set(started) == set(range(1, 33))


@pytest.mark.parametrize('duplicate', [False, True])
async def test_feedback_reports_stored_answer_and_removes_controls(monkeypatch, duplicate):
    from app import lifecycle
    cb = callback('task_feedback:10:worse')
    db = SimpleNamespace(commit=Mock())
    monkeypatch.setattr(telegram, 'SessionLocal', lambda: nullcontext(db))
    boundary = Mock(return_value=('better', duplicate)); monkeypatch.setattr(lifecycle, 'submit_step_feedback', boundary)
    project = AsyncMock(); monkeypatch.setattr(telegram, '_project_step_status', project)
    await telegram.handle_task_feedback(cb)
    cb.answer.assert_awaited_once_with('Відгук збережено: краще')
    assert boundary.call_args.kwargs['value'] == 'worse'
    db.commit.assert_called_once(); project.assert_awaited_once_with(10)
    cb.message.answer.assert_not_awaited()

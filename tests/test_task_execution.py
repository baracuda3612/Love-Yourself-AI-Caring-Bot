"""Transport contract; authoritative state/concurrency is covered by WP-03.4 PG tests."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app import telegram
from app.lifecycle import LifecycleResult, LifecycleOwnershipError, LifecycleEntitlementError, LifecycleTransitionError


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

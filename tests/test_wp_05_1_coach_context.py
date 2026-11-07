"""Regression coverage through the model request and the transactional dispatcher."""
import json
from contextlib import nullcontext
from datetime import datetime, timezone
from itertools import product
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app import access, db as models, lifecycle, orchestrator, privacy
from app.workers import coach_agent
from test_exercise_context_keyboard_cleanup import coach_db


@pytest.mark.parametrize(('mode', 'format_', 'configured', 'waiting'), product(
    ('ACTIVE', 'ACTIVE_PAUSED'), ('SHORT', 'MEDIUM'), (False, True), (False, True),
))
def test_active_coach_tool_matrix(mode, format_, configured, waiting):
    payload = {
        'access': (1, 2),
        'current_mode': mode,
        'runtime_plan_type': format_,
        'evening_slot_collected': configured,
        'evening_time': '20:30' if configured else None,
        'pending_action': 'collect_evening_time_for_switch:call-1' if waiting else None,
    }
    names = {tool['name'] for tool in coach_agent.coach_tools(payload)}
    assert ('change_evening_time' in names) == (format_ == 'MEDIUM' and configured)
    assert ('record_evening_time' in names) == (format_ == 'SHORT' and not configured and waiting)
    assert ('pause_plan' in names) == (mode == 'ACTIVE')
    assert ('resume_plan' in names) == (mode == 'ACTIVE_PAUSED')
    assert 'cancel_plan' not in names
    assert 'create_first_plan' not in names
    assert 'create_followup_plan' not in names


@pytest.fixture
def chat(monkeypatch, coach_db):
    db, _ = coach_db
    db.add(models.UserProfile(
        user_id=1, daily_time_slots={'DAY': '14:00', 'EVENING': '20:30'},
        evening_slot_collected=False,
    ))
    db.add(models.OnboardingProgress(user_id=1, stage='COMPLETED'))
    db.commit()
    live = SimpleNamespace(access=True, notice=13, pending=None)
    def pinned_notice(session, user_id, *, acknowledged):
        assert session is db and user_id == 1 and acknowledged
        if not live.access:
            raise access.AccessDenied()
        if live.notice is None:
            raise privacy.NoticeRequired()
        return SimpleNamespace(id=12), SimpleNamespace(id=live.notice)

    monkeypatch.setattr(privacy, 'pinned_notice', pinned_notice)
    monkeypatch.setattr(access, 'require_access', Mock())
    monkeypatch.setattr(orchestrator, 'SessionLocal', lambda: nullcontext(db))
    monkeypatch.setattr(models, 'SessionLocal', lambda: nullcontext(db))
    monkeypatch.setattr(orchestrator, '_auto_complete_plan_if_needed_for_user_id', AsyncMock())
    monkeypatch.setattr(orchestrator, 'get_stm_history', AsyncMock(return_value=[]))
    monkeypatch.setattr(orchestrator, 'session_memory', SimpleNamespace(
        append_message=AsyncMock(), get_pending_action=AsyncMock(side_effect=lambda _id: live.pending),
        read_pending_action=lambda _id: live.pending, clear_pending_action=AsyncMock(),
    ))
    return db, live


def tool_response(name, arguments):
    return SimpleNamespace(output=[SimpleNamespace(
        type='function_call', name=name, arguments=arguments, call_id='call_wp051',
    )], usage=None)


def provider(monkeypatch, callback):
    create = AsyncMock(side_effect=callback)
    monkeypatch.setattr(coach_agent, 'async_client', SimpleNamespace(responses=SimpleNamespace(create=create)))
    return create


@pytest.mark.parametrize(('name', 'arguments', 'offered'), [
    ('cancel_plan', '{}', False),
    ('resume_plan', '{}', False),
    ('change_evening_time', '{"hhmm":"20:30"}', False),
    ('invented_tool', '{}', False),
    ('create_first_plan', '{}', False),
    ('pause_plan', 'broken JSON', True),
    ('pause_plan', '[]', True),
    ('pause_plan', '{"user_id":2}', True),
    ('change_day_time', '{"hhmm":"24:00"}', True),
    ('change_day_time', '{"hhmm":1200}', True),
    ('change_day_time', '{}', True),
    ('switch_plan_format', '{"plan_type":"LONG"}', True),
    ('retry_plan_action', '{"action":"pause","original_source_operation_id":""}', True),
])
async def test_model_cannot_execute_hidden_tools_or_bad_arguments(chat, monkeypatch, name, arguments, offered):
    async def answer(**request):
        assert (name in {tool['name'] for tool in request['tools']}) is offered
        return tool_response(name, arguments)
    provider(monkeypatch, answer)
    handler = Mock(side_effect=AssertionError('invalid output reached the dispatcher'))
    monkeypatch.setattr(orchestrator, '_execute_plan_tool', handler)

    reply = await orchestrator.handle_incoming_message(1, 'Спробуй змінити план')
    assert reply['reply_text'] == orchestrator._INVALID_COACH_ACTION_REPLY
    handler.assert_not_called()


@pytest.mark.parametrize(('change', 'expected'), [
    ('plan', orchestrator._STALE_COACH_ACTION_REPLY),
    ('mode', orchestrator._STALE_COACH_ACTION_REPLY),
    ('evening_slot', orchestrator._STALE_COACH_ACTION_REPLY),
    ('access', orchestrator._ACCESS_DENIED_REPLY),
    ('notice', orchestrator._NOTICE_REQUIRED_REPLY),
    ('newly_acknowledged_notice', orchestrator._STALE_COACH_ACTION_REPLY),
])
async def test_live_authorization_rejects_after_model_request(chat, monkeypatch, change, expected):
    db, live = chat
    tool_name = 'change_day_time'
    if change == 'evening_slot':
        db.get(models.AIPlan, 1).total_days = 14
        db.get(models.UserProfile, 1).evening_slot_collected = True
        db.commit()
        tool_name = 'change_evening_time'

    async def answer(**request):
        assert tool_name in {tool['name'] for tool in request['tools']}
        if change == 'plan':
            plan = db.get(models.AIPlan, 1)
            plan.status = 'abandoned'
            plan.abandoned_at = datetime.now(timezone.utc)
            db.flush()
            db.add(models.AIPlan(id=2, user_id=1, title='Replacement', cycle_number=2,
                                 total_days=7, status='active', activated_at=datetime.now(timezone.utc)))
            db.commit()
        elif change == 'mode':
            db.get(models.AIPlan, 1).status = 'paused'
            db.commit()
        elif change == 'evening_slot':
            db.get(models.UserProfile, 1).evening_slot_collected = False
            db.commit()
        elif change == 'access':
            live.access = False
        else:
            live.notice = None if change == 'notice' else 14
        return tool_response(tool_name, '{"hhmm":"15:30"}')
    provider(monkeypatch, answer)
    mutation = Mock(side_effect=AssertionError('stale action reached the lifecycle'))
    monkeypatch.setattr(lifecycle, 'change_delivery_time', mutation)

    assert (await orchestrator.handle_incoming_message(1, 'Перенеси денний час на 15:30'))['reply_text'] == expected
    mutation.assert_not_called()
    assert db.get(models.UserProfile, 1).daily_time_slots['DAY'] == '14:00'


async def test_model_receives_canonical_context_and_backend_executes_offered_tool(chat, monkeypatch):
    db, _ = chat
    db.get(models.AIPlan, 1).total_days = 14
    db.get(models.UserProfile, 1).evening_slot_collected = True
    db.commit()
    async def answer(**request):
        facts = json.loads(request['input'][2]['content'].split('\n', 1)[1])
        assert facts['plan_type'] == 'MEDIUM'
        assert facts['current_mode'] == 'ACTIVE'
        assert facts['access_available'] is True
        assert (facts['current_day'], facts['total_days']) == (1, 14)
        assert facts['evening_time_configured'] is True
        assert facts['evening_time'] == '20:30'
        assert facts['pending_runtime_action'] is None
        assert facts['current_exercise_context'] is None
        assert 'current_plan_id' not in facts
        assert 'change_evening_time' in {tool['name'] for tool in request['tools']}
        return tool_response('get_plan_status', '{}')
    call = provider(monkeypatch, answer)
    reply = await orchestrator.handle_incoming_message(1, 'Який у мене план?')
    assert '14' in reply['reply_text']
    call.assert_awaited_once()


@pytest.mark.parametrize('mode', ['switch', 'followup'])
@pytest.mark.parametrize('stale_hint', [False, True])
async def test_first_evening_wait_is_bound_to_owned_intent(chat, monkeypatch, mode, stale_hint):
    db, live = chat
    if mode == 'switch':
        prefix = 'collect_evening_time_for_switch'
        operation = 'switch_plan_format'
        source = 'prior-tool-call'
    else:
        plan = db.get(models.AIPlan, 1)
        plan.status = 'abandoned'
        plan.abandoned_at = datetime.now(timezone.utc)
        prefix = 'collect_evening_time_for_medium'
        operation = 'prepare_followup_evening'
        source = lifecycle._followup_evening_source_id('prior-tool-call')
    live.pending = f'{prefix}:prior-tool-call'
    db.add(models.PlanLifecycleOperation(
        id=1, user_id=1, plan_id=1, operation=operation, source_operation_id=source,
        result_status='MEDIUM',
    ))
    db.commit()

    async def answer(**request):
        assert 'record_evening_time' in {tool['name'] for tool in request['tools']}
        facts = json.loads(request['input'][2]['content'].split('\n', 1)[1])
        assert facts['pending_runtime_action'] == prefix
        assert source not in request['input'][2]['content']
        if stale_hint:
            live.pending = None
        return tool_response('record_evening_time', '{"hhmm":"20:30"}')
    provider(monkeypatch, answer)
    if stale_hint:
        record = Mock(side_effect=AssertionError('stale wait reached the lifecycle'))
    else:
        # Probe entry through the actual runtime transaction. Stop before the
        # unrelated builder and post-commit scheduling work.
        def accept(session, *, user_id, hhmm, context, pending_source_operation_id, **_):
            assert session is db
            assert (user_id, hhmm, context, pending_source_operation_id) == (1, '20:30', mode, 'prior-tool-call')
            raise lifecycle.LifecycleTransitionError('fixture_stop')
        record = Mock(side_effect=accept)
    monkeypatch.setattr(lifecycle, 'record_evening_time_preference', record)
    reply = await orchestrator.handle_incoming_message(1, 'О 20:30')
    if stale_hint:
        assert reply['reply_text'] == orchestrator._STALE_COACH_ACTION_REPLY
        record.assert_not_called()
    else:
        record.assert_called_once()

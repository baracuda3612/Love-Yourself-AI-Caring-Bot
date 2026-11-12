"""Regression coverage through the model request and the transactional dispatcher."""
import json
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from itertools import product
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

from app import access, db as models, lifecycle, orchestrator, privacy, user_menu
from app.workers import assistant_agent
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
    names = {tool['name'] for tool in assistant_agent.assistant_tools(payload)}
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
    live = SimpleNamespace(access=True, notice=13, pending=None, delivery_enabled=True)
    def pinned_notice(session, user_id, *, acknowledged):
        assert session is db and user_id == 1 and acknowledged
        if not live.access:
            raise access.AccessDenied()
        if live.notice is None:
            raise privacy.NoticeRequired()
        return SimpleNamespace(id=12, delivery_enabled=live.delivery_enabled), SimpleNamespace(id=live.notice)

    monkeypatch.setattr(privacy, 'pinned_notice', pinned_notice)
    monkeypatch.setattr(access, 'require_access', Mock())
    monkeypatch.setattr(orchestrator, 'SessionLocal', lambda: nullcontext(db))
    monkeypatch.setattr(models, 'SessionLocal', lambda: nullcontext(db))
    monkeypatch.setattr(orchestrator, '_auto_complete_plan_if_needed', Mock(return_value=None))
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
    monkeypatch.setattr(assistant_agent, 'async_client', SimpleNamespace(responses=SimpleNamespace(create=create)))
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
    ('retry_plan_action', '{"action":"pause","original_source_operation_id":""}', False),
])
async def test_model_cannot_execute_hidden_tools_or_bad_arguments(chat, monkeypatch, name, arguments, offered):
    async def answer(**request):
        assert (name in {tool['name'] for tool in request['tools']}) is offered
        return tool_response(name, arguments)
    provider(monkeypatch, answer)
    handler = Mock(side_effect=AssertionError('invalid output reached the dispatcher'))
    monkeypatch.setattr(orchestrator, '_execute_plan_tool', handler)

    reply = await orchestrator.handle_incoming_message(1, 'Спробуй змінити план')
    assert reply['reply_text'] == orchestrator._INVALID_ASSISTANT_ACTION_REPLY
    handler.assert_not_called()


@pytest.mark.parametrize(('change', 'expected'), [
    ('plan', orchestrator._STALE_ASSISTANT_ACTION_REPLY),
    ('mode', orchestrator._STALE_ASSISTANT_ACTION_REPLY),
    ('finished_cycle', orchestrator._STALE_ASSISTANT_ACTION_REPLY),
    ('evening_slot', orchestrator._STALE_ASSISTANT_ACTION_REPLY),
    ('access', orchestrator._ACCESS_DENIED_REPLY),
    ('notice', orchestrator._NOTICE_REQUIRED_REPLY),
    ('newly_acknowledged_notice', orchestrator._STALE_ASSISTANT_ACTION_REPLY),
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
        elif change == 'finished_cycle':
            finish_current_exercises(db)
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


def finish_current_exercises(db):
    now = datetime.now(timezone.utc)
    for step in db.query(models.AIPlanStep).all():
        step.step_status = 'completed'
        step.terminal_at = now
    db.commit()


async def test_finished_cycle_with_recovery_failure_exposes_only_read_tools(chat, monkeypatch):
    from app import scheduler

    db, _ = chat
    finish_current_exercises(db)
    monkeypatch.setattr(orchestrator, '_auto_complete_plan_if_needed', Mock(side_effect=RuntimeError('builder failure')))
    wake = Mock(side_effect=RuntimeError('job store unavailable'))
    monkeypatch.setattr(scheduler, 'flag_completion_recovery', wake)

    async def answer(**request):
        facts = json.loads(request['input'][2]['content'].split('\n', 1)[1])
        assert facts['cycle_ending'] is True
        assert facts['continuation_state'] == 'retry_wait'
        assert facts['schedule']['next_reminder_at'] is None
        assert {tool['name'] for tool in request['tools']} == {'get_plan_status'}
        return tool_response('pause_plan', '{}')

    create = provider(monkeypatch, answer)
    result = await orchestrator.handle_incoming_message(1, 'Щойно виконав останню вправу. Чому немає наступної?')
    assert result['reply_text'] == orchestrator._INVALID_ASSISTANT_ACTION_REPLY
    assert db.get(models.AIPlan, 1).status == 'active'
    create.assert_awaited_once()
    wake.assert_called_once()


async def test_retry_tools_require_owned_current_receipts_and_bind_action(chat, monkeypatch):
    db, _ = chat
    db.add(models.PlanLifecycleOperation(
        id=1, user_id=1, plan_id=1, operation='resume',
        source_operation_id='prior-resume', result_status='active',
    ))
    db.commit()

    async def answer(**request):
        tools = {tool['name']: tool for tool in request['tools']}
        assert 'retry_switch_plan_format' not in tools
        assert tools['retry_plan_action']['parameters']['properties']['action']['enum'] == ['resume']
        return tool_response('retry_plan_action', '{"action":"cancel","original_source_operation_id":"prior-resume"}')

    provider(monkeypatch, answer)
    mutation = Mock(side_effect=AssertionError('hidden retry action reached the dispatcher'))
    monkeypatch.setattr(orchestrator, '_execute_plan_tool', mutation)
    assert (await orchestrator.handle_incoming_message(1, 'Повтори дію'))['reply_text'] == orchestrator._INVALID_ASSISTANT_ACTION_REPLY
    mutation.assert_not_called()


@pytest.mark.parametrize(('state', 'days', 'evening_set'), [
    ('scheduled', 14, True),
    ('scheduled', 7, False),
    ('scheduled', 7, True),
    ('scheduled', 14, False),
    ('paused', 14, True),
    ('summary_pending', 7, False),
    ('summary_failed', 7, False),
    ('delivery_disabled', 7, False),
    ('no_upcoming_reminder', 7, False),
    ('timezone_unavailable', 7, False),
])
async def test_model_receives_canonical_context_and_backend_executes_offered_tool(
    chat, monkeypatch, state, days, evening_set,
):
    db, live = chat
    plan = db.get(models.AIPlan, 1)
    user = db.get(models.User, 1)
    plan.total_days = days
    profile = user.profile
    profile.daily_time_slots = {'DAY': '13:45', 'EVENING': '20:30'}
    profile.active_days = ['FRI', 'TUE']
    profile.evening_slot_collected = evening_set
    profile.name_preference = 'Олеся'
    now = datetime.now(timezone.utc)
    future = (now + timedelta(days=1)).replace(second=0, microsecond=0)
    local = future.astimezone(ZoneInfo('Europe/Kyiv'))
    done, upcoming = (db.get(models.AIPlanStep, step_id) for step_id in (1, 2))
    done.step_status = 'completed'
    done.terminal_at = now
    upcoming.step_status = 'pending'
    upcoming.scheduled_for = now - timedelta(minutes=5) if state == 'no_upcoming_reminder' else future
    upcoming.expires_at = future + timedelta(hours=1)
    if state == 'paused':
        plan.status = 'paused'
    if state.startswith('summary_'):
        plan.cycle_number = 2
        db.flush()
        db.add(models.AIPlan(
            id=2, user_id=1, title='Previous', cycle_number=1, total_days=7,
            status='completed', activated_at=now - timedelta(days=14), end_date=now,
        ))
        db.flush()
        db.add(models.PlanCompletion(
            plan_id=2, successor_plan_id=1, report_at=now, wake_at=now,
            state='failed' if state == 'summary_failed' else 'pending',
        ))
    if state == 'delivery_disabled':
        live.delivery_enabled = False
    if state == 'timezone_unavailable':
        user.timezone = 'Invalid/Zone'
    db.commit()

    menu = user_menu.schedule_text(db, user, plan, delivery=live.delivery_enabled)
    assert 'Час: 13:45' in menu
    expected_reminder_state = 'scheduled' if state.startswith('summary_') else state
    assert ('Наступне заплановане нагадування' in menu) is (expected_reminder_state == 'scheduled')
    if expected_reminder_state == 'scheduled':
        assert f'{local:%d.%m.%Y о %H:%M}' in menu

    async def answer(**request):
        system_facts = request['input'][2]['content']
        facts = json.loads(system_facts.split('\n', 1)[1])
        assert facts['plan_type'] == ('MEDIUM' if days == 14 else 'SHORT')
        assert facts['current_mode'] == ('ACTIVE_PAUSED' if state == 'paused' else 'ACTIVE')
        assert facts['access_available'] is True
        assert (facts['current_day'], facts['total_days']) == (1, days)
        assert (facts['steps_completed'], facts['steps_total']) == (1, 2)
        assert facts['schedule'] == {
            'day_time': '13:45',
            'active_days': ['TUE', 'FRI'],
            'timezone': user.timezone,
            'reminder_state': expected_reminder_state,
            'next_reminder_at': local.isoformat(timespec='minutes') if expected_reminder_state == 'scheduled' else None,
        }
        assert facts['evening_time_configured'] is evening_set
        assert facts['evening_time'] == ('20:30' if days == 14 and evening_set else None)
        assert facts['pending_runtime_action'] is None
        assert facts['current_exercise_context'] is None
        assert 'current_plan_id' not in facts
        assert 'Олеся' not in system_facts
        assert request['input'][3]['role'] == 'user'
        assert json.loads(request['input'][3]['content']) == {'name_preference': 'Олеся'}
        assert request['input'][-1] == {'role': 'user', 'content': 'Який у мене план?'}
        tools = {tool['name'] for tool in request['tools']}
        assert ('change_evening_time' in tools) is (days == 14 and evening_set)
        return tool_response('get_plan_status', '{}')

    call = provider(monkeypatch, answer)
    reply = await orchestrator.handle_incoming_message(1, 'Який у мене план?')
    assert str(days) in reply['reply_text']
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
        assert reply['reply_text'] == orchestrator._STALE_ASSISTANT_ACTION_REPLY
        record.assert_not_called()
    else:
        record.assert_called_once()

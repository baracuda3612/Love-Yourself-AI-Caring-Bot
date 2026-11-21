"""WP-03.3 uses the WP-03.1 fresh disposable database fixture, never durable data."""
import asyncio
from concurrent.futures import Future
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from test_versioned_content_postgres import db, migrated_engine, make_draft, activate
from test_exercise_presentation_delivery import bot, presentation, rejection
from app.db import AIPlanDay, AIPlanStep, ContentLibrary, ExerciseDelivery, PlanLifecycleOperation, UserEvent
from app.exercise_presentation import current_exercise_context, step_presentation
from app.lifecycle import transition_plan_step

pytestmark = pytest.mark.skipif(
    __import__('os').environ.get('WP03_1_POSTGRES_REHEARSAL') != '1',
    reason='explicit disposable PostgreSQL rehearsal required',
)


def active_step(db):
    user, draft = make_draft(db)
    plan = activate(db,user,draft).plan
    step = db.execute(select(AIPlanStep).join(AIPlanDay).where(AIPlanDay.plan_id == plan.id).order_by(AIPlanStep.id)).scalars().first()
    step.scheduled_for = datetime.now(timezone.utc)-timedelta(minutes=1)
    step.expires_at = datetime.now(timezone.utc)+timedelta(hours=1)
    db.flush()
    return user, plan, step


@contextmanager
def retained_session(db):
    yield db


@pytest.mark.parametrize('terminal',['completed','skipped'])
async def test_coach_context_is_owned_and_receipt_proven(db,monkeypatch,terminal):
    from app import orchestrator, telegram
    user,plan,step = active_step(db)
    assert current_exercise_context(db,user.id,plan.id) is None
    step.tg_message_id=77; db.flush()
    assert current_exercise_context(db,user.id,plan.id) is None
    transition_plan_step(db,user_id=user.id,step_id=step.id,target_status='delivered',source_operation_id='wp033:test-delivery')
    context = current_exercise_context(db,user.id,plan.id)
    assert context['steps'] == list(step_presentation(db,step).steps)
    assert context['content_version'] == step.content_version
    assert context['status'] == 'delivered'
    assert current_exercise_context(db,user.id+1000,plan.id) is None
    assert current_exercise_context(db,user.id,plan.id+1000) is None
    monkeypatch.setattr(orchestrator,'SessionLocal',lambda:retained_session(db))
    monkeypatch.setattr(orchestrator,'get_stm_history',AsyncMock(return_value=[]))
    monkeypatch.setattr(orchestrator,'_assistant_access',lambda *_: ((1, 1), True))
    monkeypatch.setattr(orchestrator.session_memory,'get_pending_action',AsyncMock(return_value=None))
    payload = await orchestrator.build_user_context(user.id,'Як виконати вправу?')
    assert payload['current_exercise_context']['steps'] == context['steps']
    transition_plan_step(db,user_id=user.id,step_id=step.id,target_status=terminal,source_operation_id=f'wp033:test-{terminal}')
    monkeypatch.setattr(telegram,'SessionLocal',lambda:retained_session(db))
    callback=SimpleNamespace(message=SimpleNamespace(message_id=77,edit_reply_markup=AsyncMock()))
    assert await telegram._clear_terminal_callback_keyboard(callback,step.id)
    db.refresh(step)
    assert step.tg_message_id is None
    terminal = current_exercise_context(db,user.id,plan.id)
    assert terminal['status'] == step.step_status and terminal['available_actions'] == []
    next_message = await orchestrator.build_user_context(user.id,'Нагадай вправу')
    assert next_message['current_exercise_context']['steps'] == terminal['steps']


@pytest.mark.parametrize('outcome',['success','definite_failure','uncertain'])
def test_scheduler_uses_canonical_send_and_only_confirms_success(db,monkeypatch,outcome):
    from app import scheduler, telegram, active_days
    user,plan,step = active_step(db)
    tg = bot(chat_id=user.tg_id)
    sent_at = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=10)
    tg.send_animation.return_value.date = sent_at
    tg.send_message.return_value.date = sent_at
    if outcome == 'definite_failure':
        tg.send_animation.side_effect = rejection(); tg.send_message.side_effect = rejection()
    elif outcome == 'uncertain':
        tg.send_animation.side_effect = TimeoutError(); tg.send_message.side_effect = TimeoutError()
    monkeypatch.setattr(telegram,'bot',tg)
    monkeypatch.setattr(scheduler,'SessionLocal',lambda:retained_session(db))
    monkeypatch.setattr(scheduler,'ReceiptSessionLocal',lambda:retained_session(db))
    # The outer fixture rolls back the entire database transaction.
    monkeypatch.setattr(db,'commit',db.flush)
    monkeypatch.setattr(scheduler,'_event_loop',object())
    monkeypatch.setattr(active_days,'is_active_day',lambda *_args:True)

    def submit(coro):
        future=Future(); future.set_result(asyncio.run(coro)); return future
    monkeypatch.setattr(scheduler,'_submit_coroutine',submit)
    result=scheduler.send_scheduled_message(user.tg_id,'OLD INVALID SLOT/COUNTER/TITLE',step.id)
    assert result.outcome == {'success':'delivered','definite_failure':'failed','uncertain':'uncertain'}[outcome]
    db.refresh(step)
    receipt=db.execute(select(PlanLifecycleOperation).where(PlanLifecycleOperation.plan_step_id==step.id,PlanLifecycleOperation.operation=='step_delivered')).scalar_one_or_none()
    events=[e.event_name for e in db.query(UserEvent).filter(UserEvent.plan_step_id == step.id).all()]
    if outcome == 'success':
        assert receipt is not None and step.step_status=='delivered' and step.tg_message_id==77
        assert events==['task_delivered']
        assert db.query(ExerciseDelivery).filter_by(plan_step_id=step.id).one().confirmed_at == sent_at
        assert result.rendered_payload.startswith('Пауза\n')
        assert 'OLD INVALID' not in result.rendered_payload
        assert result.presentation.steps == step_presentation(db,step).steps
    else:
        assert receipt is None and step.step_status=='pending' and step.tg_message_id is None
        assert events==(['task_delivery_failed'] if outcome=='definite_failure' else [])


def test_old_persisted_job_signature_and_new_job_has_no_baked_copy(db,monkeypatch):
    from app import scheduler
    user,plan,step=active_step(db)
    step.scheduled_for=datetime.now(timezone.utc)+timedelta(hours=1)
    step.expires_at=step.scheduled_for+timedelta(hours=1)
    jobs=[]
    monkeypatch.setattr(scheduler.scheduler,'add_job',lambda *args,**kwargs:jobs.append((args,kwargs)))
    assert scheduler.schedule_plan_step(step,user)
    assert jobs[0][0]==('app.scheduler:send_scheduled_message','date')
    assert jobs[0][1]['args']==[user.tg_id,'',step.id]


def test_db_gated_content_cannot_leak_to_coach(db):
    user,plan,step=active_step(db)
    step.tg_message_id=77
    transition_plan_step(db,user_id=user.id,step_id=step.id,target_status='delivered',source_operation_id='wp033:test-gated')
    content=db.get(ContentLibrary,(step.exercise_id,step.content_version))
    content.is_active=False; db.flush()
    assert current_exercise_context(db,user.id,plan.id) is None


@pytest.mark.parametrize('change', ['none', 'entitlement', 'notice', 'first_evening'])
async def test_coach_tool_rechecks_real_postgres_authority(db, monkeypatch, change):
    from dataclasses import replace
    from unittest.mock import Mock
    from app import access, db as models, lifecycle, lifecycle_reconciliation, orchestrator, privacy
    from app.workers import assistant_agent

    user, plan, _ = active_step(db)
    enrollment = access.require_access(db, user.id)
    deployment = db.get(models.Deployment, enrollment.deployment_id)
    notice = privacy.register_notice(db, version='wp051-notice', notice_text='Fixture', location='test')
    deployment.notice_version_id = notice.id
    db.flush()
    privacy.acknowledge_notice(db, user.id, notice.id, 'fixture:ack')
    pending = None
    if change == 'first_evening':
        switch = lifecycle.switch_plan_format(
            db, user_id=user.id, target_plan_type='MEDIUM', source_operation_id='wp051-switch',
        )
        assert switch.code == 'needs_evening_time'
        pending = 'collect_evening_time_for_switch:wp051-switch'
    db.commit()

    monkeypatch.setattr(orchestrator, 'SessionLocal', lambda: retained_session(db))
    monkeypatch.setattr(models, 'SessionLocal', lambda: retained_session(db))
    monkeypatch.setattr(orchestrator, '_auto_complete_plan_if_needed_for_user_id', AsyncMock())
    monkeypatch.setattr(orchestrator, 'get_stm_history', AsyncMock(return_value=[]))
    memory = SimpleNamespace(
        append_message=AsyncMock(), get_pending_action=AsyncMock(return_value=pending),
        read_pending_action=lambda _: pending, clear_pending_action=AsyncMock(),
    )
    monkeypatch.setattr(orchestrator, 'session_memory', memory)
    reconcile = Mock(side_effect=lambda result: replace(result, effects=tuple(
        replace(effect, state=lifecycle.ExternalEffectState.SUCCEEDED) for effect in result.effects
    )))
    monkeypatch.setattr(lifecycle_reconciliation, 'reconcile_scheduler_effects', reconcile)

    async def respond(**request):
        tool_name = 'record_evening_time' if pending else 'pause_plan'
        assert tool_name in {tool['name'] for tool in request['tools']}
        if change == 'entitlement':
            access.revoke_entitlement(db, entitlement_id=enrollment.entitlement_id)
        elif change == 'notice':
            replacement = privacy.register_notice(
                db, version='wp051-replacement', notice_text='Updated fixture', location='test',
            )
            deployment.notice_version_id = replacement.id
        db.commit()
        return SimpleNamespace(output=[SimpleNamespace(
            type='function_call', name=tool_name, call_id='call_wp051',
            arguments='{"hhmm":"20:30"}' if pending else '{}',
        )], usage=None)

    monkeypatch.setattr(assistant_agent, 'async_client', SimpleNamespace(
        responses=SimpleNamespace(create=AsyncMock(side_effect=respond)),
    ))
    message = 'О 20:30' if pending else 'Призупини план'
    reply = (await orchestrator.handle_incoming_message(user.id, message))['reply_text']
    receipt = lifecycle.find_lifecycle_operation(db, user.id, 'call_wp051')
    db.refresh(plan)
    if change == 'first_evening':
        assert 'Формат змінено' in reply
        current = lifecycle.get_current_plan(db, user.id)
        assert (current.status, current.total_days) == ('active', 14)
        assert plan.status == 'abandoned' and receipt.plan_id == plan.id
        assert user.profile.evening_slot_collected is True
        assert user.profile.daily_time_slots['EVENING'] == '20:30'
        memory.clear_pending_action.assert_awaited_once_with(user.id)
    elif change == 'none':
        assert 'паузу' in reply
        assert plan.status == 'paused' and receipt.plan_id == plan.id
        reconcile.assert_called_once()
    else:
        expected = orchestrator._ACCESS_DENIED_REPLY if change == 'entitlement' else orchestrator._NOTICE_REQUIRED_REPLY
        assert reply == expected
        assert plan.status == 'active' and receipt is None
        reconcile.assert_not_called()

"""Opt-in real migrated, fresh disposable PG: send journal, actions and UI."""
import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from test_versioned_content_postgres import migrated_engine, db
from test_wp_03_3_postgres_presentation import active_step
from app.db import AIPlanStep, ExerciseDelivery, FeedbackEvent, UserEvent, User
from app.exercise_delivery import ExerciseSendResult
from app.lifecycle import transition_owned_plan_step, submit_step_feedback, transition_current_plan, abandon_current_plan, expire_plan_step, LifecycleTransitionError, LifecycleOwnershipError
from app.scheduled_delivery import claim_send, finish_send, latest_attempt, sent_receipt, utc
from app.ux.exercise_renderer import render_exercise

pytestmark = pytest.mark.skipif(__import__('os').environ.get('WP03_1_POSTGRES_REHEARSAL') != '1', reason='requires fresh disposable PG')


def success(claim, variant='text'):
    return ExerciseSendResult('delivered', variant, claim.presentation, render_exercise(claim.presentation, caption=variant=='gif'), claim.chat_id, 777)


def deliver(db, step, variant='text'):
    claim = claim_send(db, step.id); db.flush()
    finish_send(db, claim.attempt_id, success(claim, variant)); db.flush()
    return claim


def events(db, step):
    return db.query(UserEvent).filter(UserEvent.plan_step_id == step.id).all()


def action(db, user, step, target, **kwargs):
    return transition_owned_plan_step(db, telegram_user_id=user.tg_id, step_id=step.id, target_status=target, source_operation_id='tap:'+uuid4().hex, **kwargs)


@pytest.mark.parametrize('variant', ['text','gif'])
def test_receipt_is_exact_immutable_once_and_not_process_cache(db, variant):
    user, plan, step = active_step(db)
    claim = claim_send(db, step.id)
    assert not events(db, step)
    assert claim_send(db, step.id) is None
    result = success(claim, variant)
    finish_send(db, claim.attempt_id, result); db.flush()
    receipt = sent_receipt(db, step.id)
    assert receipt.presentation_snapshot == result.presentation.to_payload()
    assert (receipt.variant, receipt.rendered_payload, receipt.chat_id, receipt.message_id) == (variant, result.rendered_payload, user.tg_id, 777)
    assert step.step_status == 'delivered'
    finish_send(db, claim.attempt_id, result)
    assert claim_send(db, step.id) is None
    assert [e.event_name for e in events(db, step)] == ['task_delivered']
    with pytest.raises(DBAPIError), db.begin_nested():
        receipt.rendered_payload = 'changed'; db.flush()


@pytest.mark.parametrize('outcome', ['uncertain','crash'])
def test_unknown_send_quarantines_after_restart_and_late_receipt_recovers(db, outcome):
    _, _, step = active_step(db)
    claim = claim_send(db, step.id)
    if outcome == 'uncertain':
        finish_send(db, claim.attempt_id, ExerciseSendResult('uncertain','text',claim.presentation,'x',failure_code='timeout'))
    else:
        claim_send(db, step.id, now=datetime.now(timezone.utc)+timedelta(minutes=3))
    assert latest_attempt(db,step.id).state == 'uncertain'
    assert claim_send(db, step.id) is None and not events(db, step)
    finish_send(db, claim.attempt_id, success(claim))
    assert step.step_status == 'delivered' and len(events(db,step)) == 1


def test_rate_limit_has_three_bounded_attempts_with_one_source(db):
    _, _, step = active_step(db)
    now = datetime.now(timezone.utc)
    for number in range(1,4):
        claim = claim_send(db, step.id, now=now)
        assert claim is not None
        finish_send(db, claim.attempt_id, ExerciseSendResult('failed','text',claim.presentation,render_exercise(claim.presentation),failure_code='rate_limit',retry_after=30), now=now)
        receipt = latest_attempt(db, step.id)
        assert receipt.attempt == number
        assert receipt.source_operation_id == f'scheduler:delivery:{step.id}'
        assert receipt.state == ('retryable' if number < 3 else 'terminal_failure')
        assert claim_send(db, step.id, now=now+timedelta(seconds=1)) is None
        now += timedelta(seconds=31)
    assert [e.event_name for e in events(db,step)] == ['task_delivery_failed']*3
    assert step.step_status == 'pending'


@pytest.mark.parametrize('scenario', ['permanent','grace','expiry','invalid_content'])
def test_terminal_failures_never_send_opportunity(db, scenario):
    _, _, step = active_step(db)
    if scenario == 'invalid_content':
        db.execute(text("UPDATE content_library SET is_active=false WHERE exercise_id=:id AND content_version=:v"), {'id':step.exercise_id,'v':step.content_version})
        assert claim_send(db,step.id) is None
    else:
        claim = claim_send(db, step.id)
        result = ExerciseSendResult('failed','text',claim.presentation,'x',failure_code='reject',retry_after=None if scenario=='permanent' else 30)
        finish_send(db, claim.attempt_id, result)
        if scenario != 'permanent':
            at = utc(step.scheduled_for)+timedelta(hours=3) if scenario=='grace' else utc(step.expires_at)+timedelta(seconds=1)
            assert claim_send(db,step.id,now=at) is None
    assert latest_attempt(db,step.id).state == 'terminal_failure'
    assert all(e.event_name != 'task_delivered' for e in events(db,step))


@pytest.mark.parametrize('state', ['paused','abandoned'])
def test_pause_cancel_during_send_preserves_fact_and_lifecycle(db, state):
    user, plan, step = active_step(db)
    claim=claim_send(db,step.id)
    if state=='paused': transition_current_plan(db,user_id=user.id,operation='pause',source_operation_id='control:'+state)
    else: abandon_current_plan(db,user_id=user.id,source_operation_id='control:'+state)
    finish_send(db,claim.attempt_id,success(claim)); db.flush()
    assert sent_receipt(db,step.id)
    assert step.step_status == ('delivered' if state=='paused' else 'canceled')
    result=action(db,user,step,'completed')
    assert result.status == ('completed' if state=='paused' else 'canceled')
    assert len([e for e in events(db,step) if e.event_name=='task_delivered']) == 1


@pytest.mark.parametrize('target', ['completed','skipped'])
def test_paused_delivered_actions_remain_live_and_replays_are_factual(db,target):
    user,plan,step=active_step(db); deliver(db,step)
    transition_current_plan(db,user_id=user.id,operation='pause',source_operation_id='pause')
    first=action(db,user,step,target); assert first.status==target
    again=action(db,user,step,'skipped' if target=='completed' else 'completed')
    assert again.duplicate and again.status==target
    assert len([e for e in events(db,step) if e.event_name in ('task_completed','task_skipped')]) == 1


@pytest.mark.parametrize('state', ['pending','delivered'])
def test_late_tap_atomically_expires_including_paused(db,state):
    user,plan,step=active_step(db)
    if state=='delivered': deliver(db,step)
    step.expires_at=datetime.now(timezone.utc)-timedelta(seconds=1); db.flush()
    transition_current_plan(db,user_id=user.id,operation='pause',source_operation_id='pause')
    result=action(db,user,step,'completed')
    assert result.status=='expired' and step.step_status=='expired'
    assert not any(e.event_name=='task_completed' for e in events(db,step))
    assert sum(e.event_name=='task_ignored' for e in events(db,step))==1


def test_pending_and_cross_user_actions_and_feedback_rejected(db):
    user,plan,step=active_step(db)
    with pytest.raises(LifecycleTransitionError, match='step_not_delivered'): action(db,user,step,'completed')
    stranger=User(tg_id=9888777,is_active=True,timezone='UTC'); db.add(stranger); db.flush()
    with pytest.raises(LifecycleOwnershipError): action(db,stranger,step,'completed')
    with pytest.raises(LifecycleTransitionError,match='requires_completed'):
        submit_step_feedback(db,telegram_user_id=user.tg_id,step_id=step.id,value='better')
    deliver(db,step); action(db,user,step,'skipped')
    with pytest.raises(LifecycleTransitionError,match='requires_completed'):
        submit_step_feedback(db,telegram_user_id=user.tg_id,step_id=step.id,value='better')


@pytest.mark.parametrize('value', ['better','same','worse'])
def test_completed_feedback_is_immutable_content_linked_and_atomic(db,value):
    user,_,step=active_step(db); deliver(db,step); action(db,user,step,'completed')
    assert submit_step_feedback(db,telegram_user_id=user.tg_id,step_id=step.id,value=value)==(value,False)
    assert submit_step_feedback(db,telegram_user_id=user.tg_id,step_id=step.id,value='worse')==(value,True)
    row=db.query(FeedbackEvent).filter_by(plan_step_id=step.id).one()
    assert row.context=={'exercise_id':step.exercise_id,'content_version':step.content_version}
    assert sum(e.event_name=='feedback_submitted' for e in events(db,step))==1
    with pytest.raises(DBAPIError), db.begin_nested():
        row.value='changed'; db.flush()


def test_feedback_event_failure_rolls_back_feedback(db,monkeypatch):
    from app import telemetry
    user,_,step=active_step(db); deliver(db,step); action(db,user,step,'completed')
    monkeypatch.setattr(telemetry,'write_event_operation',lambda *_a,**_k: (_ for _ in ()).throw(RuntimeError('event failure')))
    with pytest.raises(RuntimeError), db.begin_nested():
        submit_step_feedback(db,telegram_user_id=user.tg_id,step_id=step.id,value='better')
    assert db.query(FeedbackEvent).filter_by(plan_step_id=step.id).count()==0


@pytest.mark.parametrize('variant', ['text','gif'])
@pytest.mark.parametrize('target', ['completed','skipped','expired','canceled'])
def test_terminal_text_caption_status_and_feedback_with_durable_ui_retry(db,monkeypatch,variant,target):
    from app import exercise_status
    user,_,step=active_step(db); claim=deliver(db,step,variant)
    if target=='canceled': abandon_current_plan(db,user_id=user.id,source_operation_id='cancel')
    elif target=='expired': expire_plan_step(db,user_id=user.id,step_id=step.id,source_operation_id='expiry',occurred_at=datetime.now(timezone.utc))
    else: action(db,user,step,target)
    class FakeBot:
        def __init__(self,**kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        edit_message_caption=AsyncMock(); edit_message_text=AsyncMock()
    monkeypatch.setattr(exercise_status,'Bot',FakeBot)
    edit=FakeBot.edit_message_caption if variant=='gif' else FakeBot.edit_message_text
    edit.side_effect=TimeoutError()
    assert exercise_status.reconcile_status(db,step.id) is False
    assert sent_receipt(db,step.id).visible_status is None
    edit.side_effect=None
    monkeypatch.setattr(db,'commit',db.flush)
    assert exercise_status.reconcile_status(db,step.id) is True
    args=edit.call_args.kwargs
    assert (args['chat_id'],args['message_id'])==(claim.chat_id,777)
    assert args.get('caption',args.get('text'))==render_exercise(replace(claim.presentation,status=target,available_actions=()),caption=variant=='gif')
    assert bool(args['reply_markup']) == (target=='completed')
    assert step.tg_message_id is None and sent_receipt(db,step.id).message_id==777
    if target=='completed':
        submit_step_feedback(db,telegram_user_id=user.tg_id,step_id=step.id,value='same')
        exercise_status.reconcile_status(db,step.id)
        assert edit.call_args.kwargs['reply_markup'] is None


@pytest.mark.parametrize('targets', [('completed','completed'),('skipped','skipped'),('completed','skipped')])
def test_real_concurrent_actions_accept_one_outcome(migrated_engine, targets):
    with Session(migrated_engine) as s:
        user,plan,step=active_step(s); deliver(s,step); s.commit(); ids=(user.tg_id,step.id,user.id)
    barrier=Barrier(2)
    def run(target):
        with Session(migrated_engine) as s:
            barrier.wait()
            result=transition_owned_plan_step(s,telegram_user_id=ids[0],step_id=ids[1],target_status=target,source_operation_id='race:'+uuid4().hex)
            s.commit(); return result
    try:
        with ThreadPoolExecutor(2) as pool: results=list(pool.map(run,targets))
        assert sum(not r.duplicate for r in results)==1
        assert results[0].status==results[1].status
        with Session(migrated_engine) as s:
            assert s.query(UserEvent).filter(UserEvent.plan_step_id==ids[1],UserEvent.event_name.in_(['task_completed','task_skipped'])).count()==1
    finally:
        with Session(migrated_engine) as s:
            s.execute(text('TRUNCATE users CASCADE')); s.commit()


def test_real_concurrent_claim_and_feedback_have_one_winner(migrated_engine):
    with Session(migrated_engine) as s:
        user,_,step=active_step(s); s.commit(); ids=(user.tg_id,step.id,user.id)
    barrier=Barrier(2)
    def claim(_):
        with Session(migrated_engine) as s:
            barrier.wait(); result=claim_send(s,ids[1]); s.commit(); return result
    try:
        with ThreadPoolExecutor(2) as pool: claims=list(pool.map(claim,range(2)))
        assert sum(c is not None for c in claims)==1
        winning=next(c for c in claims if c)
        with Session(migrated_engine) as s:
            finish_send(s,winning.attempt_id,success(winning)); transition_owned_plan_step(s,telegram_user_id=ids[0],step_id=ids[1],target_status='completed',source_operation_id='complete'); s.commit()
        barrier=Barrier(2)
        def feedback(value):
            with Session(migrated_engine) as s:
                barrier.wait(); result=submit_step_feedback(s,telegram_user_id=ids[0],step_id=ids[1],value=value); s.commit(); return result
        with ThreadPoolExecutor(2) as pool: results=list(pool.map(feedback,['better','worse']))
        assert results[0][0]==results[1][0] and sum(not r[1] for r in results)==1
    finally:
        with Session(migrated_engine) as s:
            s.execute(text('TRUNCATE users CASCADE')); s.commit()


def test_unknown_send_owned_callback_proves_delivery_and_action(db):
    from html import unescape
    import re
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    user,_,step=active_step(db); claim=claim_send(db,step.id)
    finish_send(db,claim.attempt_id,ExerciseSendResult('uncertain',None,claim.presentation,'',failure_code='timeout'))
    payload=render_exercise(claim.presentation)
    message=SimpleNamespace(chat=SimpleNamespace(id=user.tg_id,type='private'),date=datetime.now(timezone.utc),message_id=777,
                            animation=None,text=unescape(re.sub(r'</?b>','',payload)),
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                InlineKeyboardButton(text='Done',callback_data=f'task_complete:{step.id}'),
                                InlineKeyboardButton(text='Skip',callback_data=f'task_skip:{step.id}')]]))
    result=action(db,user,step,'completed',telegram_message=message)
    assert result.status=='completed'
    assert sent_receipt(db,step.id).rendered_payload==payload
    assert [e.event_name for e in events(db,step)]==['task_delivered','task_completed']
    finish_send(db,claim.attempt_id,success(claim))
    assert len(events(db,step))==2


@pytest.mark.parametrize('bad', ['wrong_chat','wrong_text','missing_buttons'])
def test_unknown_callback_requires_matching_original_message(db,bad):
    user,_,step=active_step(db); claim=claim_send(db,step.id)
    message=SimpleNamespace(chat=SimpleNamespace(id=user.tg_id+1 if bad=='wrong_chat' else user.tg_id,type='private'),date=datetime.now(timezone.utc),message_id=777,animation=None,text='wrong',reply_markup=None)
    with pytest.raises(LifecycleTransitionError,match='step_not_delivered'):
        action(db,user,step,'completed',telegram_message=message)
    assert sent_receipt(db,step.id) is None and not events(db,step)


def test_scheduler_wait_timeout_preserves_late_receipt_without_duplicate(db,monkeypatch):
    from app import scheduler, telegram, active_days
    from test_exercise_presentation_delivery import bot
    user,_,step=active_step(db)
    monkeypatch.setattr(scheduler,'SessionLocal',lambda:nullcontext(db))
    monkeypatch.setattr(db,'commit',db.flush)
    monkeypatch.setattr(scheduler,'_maybe_schedule_plan_completion',lambda *_a:None)
    monkeypatch.setattr(scheduler,'_event_loop',object())
    monkeypatch.setattr(active_days,'is_active_day',lambda *_a:True)
    tg=bot(chat_id=user.tg_id); monkeypatch.setattr(telegram,'bot',tg)
    pending=[]
    class UnknownFuture:
        def result(self,**kwargs): raise TimeoutError()
    def submit(coro): pending.append(coro); return UnknownFuture()
    monkeypatch.setattr(scheduler,'_submit_coroutine',submit)
    result=scheduler.send_scheduled_message(0,'',step.id)
    assert result.outcome=='uncertain' and not events(db,step)
    assert scheduler.send_scheduled_message(0,'',step.id) is None
    asyncio.run(pending.pop())
    assert sent_receipt(db,step.id) is not None
    assert tg.send_message.await_count+tg.send_animation.await_count==1
    assert len(events(db,step))==1


def test_restart_sweep_restores_past_due_retry_and_ui(db,monkeypatch):
    from app import scheduler, active_days, telegram, exercise_status
    from test_exercise_presentation_delivery import bot
    user,_,step=active_step(db)
    claim=claim_send(db,step.id)
    now=datetime.now(timezone.utc)-timedelta(seconds=60)
    finish_send(db,claim.attempt_id,ExerciseSendResult('failed','text',claim.presentation,'x',failure_code='limit',retry_after=30),now=now)
    monkeypatch.setattr(scheduler,'SessionLocal',lambda:nullcontext(db)); monkeypatch.setattr(db,'commit',db.flush)
    monkeypatch.setattr(scheduler,'_maybe_schedule_plan_completion',lambda *_a:None)
    monkeypatch.setattr(scheduler,'_maybe_schedule_plan_completion',lambda *_a:None)
    monkeypatch.setattr(scheduler,'_event_loop',object()); monkeypatch.setattr(active_days,'is_active_day',lambda *_a:True)
    tg=bot(chat_id=user.tg_id); monkeypatch.setattr(telegram,'bot',tg)
    def submit(coro):
        future=Future(); future.set_result(asyncio.run(coro)); return future
    monkeypatch.setattr(scheduler,'_submit_coroutine',submit)
    scheduler.reconcile_scheduled_deliveries()
    assert latest_attempt(db,step.id).attempt==2 and sent_receipt(db,step.id)
    action(db,user,step,'completed')
    class FakeBot:
        def __init__(self,**kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        edit_message_text=AsyncMock(); edit_message_caption=AsyncMock()
    monkeypatch.setattr(exercise_status,'Bot',FakeBot)
    scheduler.reconcile_scheduled_deliveries()
    assert sent_receipt(db,step.id).visible_status=='completed'
    assert FakeBot.edit_message_text.await_count+FakeBot.edit_message_caption.await_count==1


@pytest.mark.parametrize('boundary', ['send','action'])
def test_event_failure_rolls_back_authoritative_write(db,monkeypatch,boundary):
    from app import telemetry, scheduled_delivery
    user,_,step=active_step(db)
    if boundary=='action': deliver(db,step)
    else: claim=claim_send(db,step.id)
    def fail(*args,**kwargs): raise RuntimeError('event write unavailable')
    monkeypatch.setattr(telemetry,'write_event_operation',fail)
    monkeypatch.setattr(scheduled_delivery,'write_event_operation',fail)
    with pytest.raises(RuntimeError), db.begin_nested():
        if boundary=='action': action(db,user,step,'completed')
        else: finish_send(db,claim.attempt_id,success(claim))
    db.refresh(step)
    assert step.step_status==('delivered' if boundary=='action' else 'pending')
    assert not any(e.event_name=='task_completed' for e in events(db,step))
    if boundary=='send': assert latest_attempt(db,step.id).state=='in_flight' and not events(db,step)


@pytest.mark.parametrize('missing', ['variant','message_id','rendered_payload','confirmed_at'])
def test_direct_delivered_insert_requires_complete_receipt(db,missing):
    user,_,step=active_step(db)
    row=dict(plan_step_id=step.id,source_operation_id=f'scheduler:delivery:{step.id}',attempt=1,
             state='delivered',chat_id=user.tg_id,variant='text',message_id=77,rendered_payload='payload',
             started_at=datetime.now(timezone.utc),confirmed_at=datetime.now(timezone.utc),presentation_snapshot={})
    row[missing]=None
    with pytest.raises(DBAPIError), db.begin_nested(): db.add(ExerciseDelivery(**row)); db.flush()


def test_action_cancel_race_and_expiry_have_one_terminal_fact(migrated_engine):
    with Session(migrated_engine) as s:
        user,_,step=active_step(s); deliver(s,step); s.commit(); ids=(user.tg_id,step.id,user.id)
    barrier=Barrier(2)
    def run(target):
        with Session(migrated_engine) as s:
            barrier.wait()
            if target=='cancel': result=abandon_current_plan(s,user_id=ids[2],source_operation_id='cancel')[0]
            else: result=transition_owned_plan_step(s,telegram_user_id=ids[0],step_id=ids[1],target_status='completed',source_operation_id='done')
            s.commit(); return result
    try:
        with ThreadPoolExecutor(2) as pool: results=list(pool.map(run,['cancel','done']))
        with Session(migrated_engine) as s:
            step=s.get(AIPlanStep,ids[1]); count=s.query(UserEvent).filter(UserEvent.plan_step_id==ids[1],UserEvent.event_name=='task_completed').count()
            assert count==(1 if step.step_status=='completed' else 0)
            assert step.step_status in ('completed','canceled')
    finally:
        with Session(migrated_engine) as s: s.execute(text('TRUNCATE users CASCADE')); s.commit()


@pytest.mark.parametrize('variant', ['text', 'gif'])
@pytest.mark.parametrize('tool_name', ['cancel_plan', 'switch_plan_format', 'retry_plan_action'])
async def test_coach_control_projects_terminal_message_before_return(migrated_engine, monkeypatch, variant, tool_name):
    """Use real orchestrator -> runtime -> lifecycle -> projection; only Telegram/jobs are fake."""
    from threading import get_ident
    from sqlalchemy.orm import sessionmaker
    from app import db as database, orchestrator, scheduler, exercise_status
    from app.plan_runtime import tools
    from app.db import UserProfile
    with Session(migrated_engine) as db:
        user, _, step = active_step(db)
        profile = db.query(UserProfile).filter_by(user_id=user.id).one()
        profile.daily_time_slots = {'DAY': '14:00', 'EVENING': '20:30'}
        profile.evening_slot_collected = True
        claim = deliver(db, step, variant)
        db.commit()
        user_id, step_id = user.id, step.id
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False)
    monkeypatch.setattr(database, 'SessionLocal', factory)
    monkeypatch.setattr(scheduler, 'SessionLocal', factory)
    monkeypatch.setattr(orchestrator, 'SessionLocal', factory)
    jobs = {}
    class FakeScheduler:
        def remove_job(self, job_id, **kwargs): jobs.pop(job_id, None)
        def get_job(self, job_id, **kwargs): return jobs.get(job_id)
        def modify_job(self, job_id, **kwargs): jobs[job_id].next_run_time = kwargs['next_run_time']
        def add_job(self, *args, **kwargs): jobs[kwargs['id']] = SimpleNamespace(next_run_time=kwargs.get('run_date'))
    monkeypatch.setattr(scheduler, 'scheduler', FakeScheduler())
    edits = []
    polling_thread = get_ident()
    reject_edit = [tool_name == 'retry_plan_action']
    class FakeBot:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def edit_message_text(self, **kwargs):
            assert get_ident() != polling_thread
            if reject_edit[0]: raise TimeoutError('temporary edit failure')
            edits.append(('text', kwargs))
        async def edit_message_caption(self, **kwargs):
            assert get_ident() != polling_thread
            if reject_edit[0]: raise TimeoutError('temporary edit failure')
            edits.append(('gif', kwargs))
    monkeypatch.setattr(exercise_status, 'Bot', FakeBot)
    monkeypatch.setattr(orchestrator, 'log_metric', lambda *_a, **_k: None)
    try:
        args = {'plan_type': 'MEDIUM'} if tool_name == 'switch_plan_format' else {}
        if tool_name == 'retry_plan_action':
            first = await asyncio.to_thread(tools.cancel_plan, user_id, source_operation_id='cancel-before-retry')
            assert first['keyboard_cleanup_pending'] is True and not edits
            reject_edit[0] = False
            args = {'action': 'cancel', 'original_source_operation_id': 'cancel-before-retry'}
        reply = await orchestrator._execute_plan_tool(user_id, {
            'name': tool_name, 'arguments': args, 'call_id': 'wp034:' + tool_name,
        })
        assert reply and 'Не вдалося прибрати' not in reply and 'Не вдалось виконати' not in reply
        assert len(edits) == 1
        kind, kwargs = edits[0]
        assert kind == variant and kwargs['reply_markup'] is None
        assert (kwargs['chat_id'], kwargs['message_id']) == (claim.chat_id, 777)
        assert 'Скасовано' in kwargs.get('text', kwargs.get('caption'))
        with Session(migrated_engine) as db:
            assert db.get(AIPlanStep, step_id).step_status == 'canceled'
            assert sent_receipt(db, step_id).visible_status == 'canceled'
    finally:
        with Session(migrated_engine) as db:
            db.execute(text('TRUNCATE users CASCADE')); db.commit()


@pytest.mark.parametrize('variant', ['text', 'gif'])
def test_recovery_only_locks_stale_ui_and_stops_when_repaired(db, monkeypatch, variant):
    from app import scheduler, exercise_status
    from app.db import AIPlanDay
    from app.lifecycle import transition_plan_step
    user, plan, _ = active_step(db)
    steps = db.query(AIPlanStep).join(AIPlanDay).filter(AIPlanDay.plan_id == plan.id).order_by(AIPlanStep.id).all()
    assert len(steps) == 7
    statuses = ['completed', 'skipped', 'expired', 'canceled', 'completed', 'completed', 'delivered']
    for i, (step, status) in enumerate(zip(steps, statuses)):
        step.scheduled_for = datetime.now(timezone.utc) - timedelta(minutes=1)
        step.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        db.flush(); deliver(db, step, variant)
        if status != 'delivered':
            transition_plan_step(db, user_id=user.id, step_id=step.id, target_status=status,
                                 source_operation_id='history:' + str(step.id))
        if i < 4 or i == 5:
            sent_receipt(db, step.id).visible_status = status
    submit_step_feedback(db, telegram_user_id=user.tg_id, step_id=steps[5].id, value='better')
    transition_current_plan(db, user_id=user.id, operation='pause', source_operation_id='pause-history')
    db.flush()
    jobs = {}
    class FakeScheduler:
        def get_job(self, job_id, **kwargs): return jobs.get(job_id)
        def modify_job(self, job_id, **kwargs): jobs[job_id].next_run_time = kwargs['next_run_time']
        def add_job(self, *args, **kwargs): jobs[kwargs['id']] = SimpleNamespace(next_run_time=kwargs.get('next_run_time', kwargs.get('run_date')), trigger=args[1], jobstore=kwargs.get('jobstore'))
        def remove_job(self, job_id, **kwargs): jobs.pop(job_id, None)
    monkeypatch.setattr(scheduler, 'scheduler', FakeScheduler())
    monkeypatch.setattr(scheduler, 'SessionLocal', lambda: nullcontext(db))
    monkeypatch.setattr(db, 'commit', db.flush)
    locks = []
    real_lock = exercise_status._lock_user
    def lock(*args, **kwargs):
        locks.append(args[1]); return real_lock(*args, **kwargs)
    monkeypatch.setattr(exercise_status, '_lock_user', lock)
    calls = []
    fail_once = [True]
    class FakeBot:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def edit_message_text(self, **kwargs):
            calls.append(kwargs)
            if fail_once[0]:
                fail_once[0] = False
                raise TimeoutError('temporary edit failure')
        edit_message_caption = edit_message_text
    monkeypatch.setattr(exercise_status, 'Bot', FakeBot)
    assert set(exercise_status.pending_status_step_ids(db)) == {steps[4].id, steps[5].id}
    scheduler.reconcile_scheduled_deliveries()
    assert len(locks) == 2 and len(calls) == 2
    assert 'scheduled_delivery_recovery' in jobs
    scheduler.reconcile_scheduled_deliveries()
    assert len(locks) == 3 and len(calls) == 3
    assert 'scheduled_delivery_recovery' not in jobs
    scheduler.reconcile_scheduled_deliveries()
    assert len(locks) == 3 and len(calls) == 3
    assert steps[6].step_status == 'delivered' and plan.status == 'paused'
    # A later answer on a previously aligned completed message wakes only that
    # message; NULL/NULL feedback on the remaining history stays aligned.
    submit_step_feedback(db, telegram_user_id=user.tg_id, step_id=steps[4].id, value='same')
    assert exercise_status.pending_status_step_ids(db) == [steps[4].id]
    scheduler.reconcile_scheduled_deliveries()
    assert len(locks) == 4 and len(calls) == 4
    assert not jobs and not exercise_status.pending_status_step_ids(db)


def test_recovery_is_dormant_for_success_and_wakes_for_definite_retry(db, monkeypatch):
    from app import scheduler, active_days, telegram
    from test_exercise_presentation_delivery import bot
    user, _, step = active_step(db)
    jobs = {}
    class FakeScheduler:
        def get_job(self, job_id, **kwargs): return jobs.get(job_id)
        def modify_job(self, job_id, **kwargs): jobs[job_id].next_run_time = kwargs['next_run_time']
        def add_job(self, *args, **kwargs): jobs[kwargs['id']] = SimpleNamespace(next_run_time=kwargs.get('next_run_time', kwargs.get('run_date')), trigger=args[1], jobstore=kwargs.get('jobstore'))
        def remove_job(self, job_id, **kwargs): jobs.pop(job_id, None)
    monkeypatch.setattr(scheduler, 'scheduler', FakeScheduler())
    monkeypatch.setattr(scheduler, 'SessionLocal', lambda: nullcontext(db))
    monkeypatch.setattr(db, 'commit', db.flush)
    monkeypatch.setattr(scheduler, '_maybe_schedule_plan_completion', lambda *_a: None)
    monkeypatch.setattr(scheduler, '_event_loop', object())
    monkeypatch.setattr(active_days, 'is_active_day', lambda *_a: True)
    monkeypatch.setattr(telegram, 'bot', bot(chat_id=user.tg_id))
    def submit(coro):
        future = Future(); future.set_result(asyncio.run(coro)); return future
    monkeypatch.setattr(scheduler, '_submit_coroutine', submit)
    scheduler.send_scheduled_message(0, '', step.id)
    assert not jobs
    # Rehearse a separate delivery's definite failure, then two retries. Only
    # the newest failed attempt remains recovery work after it succeeds.
    from app.db import AIPlanDay
    other = db.query(AIPlanStep).join(AIPlanDay).filter(AIPlanDay.plan_id == step.day.plan_id, AIPlanStep.id != step.id).first()
    other.scheduled_for = step.scheduled_for; other.expires_at = step.expires_at; db.flush()
    remaining = [2]
    async def send(_bot, _chat, presentation, **kwargs):
        if remaining[0]:
            remaining[0] -= 1
            return ExerciseSendResult('failed', 'text', presentation, 'x', failure_code='rate_limit', retry_after=30)
        return ExerciseSendResult('delivered', 'text', presentation, render_exercise(presentation), user.tg_id, 888)
    monkeypatch.setattr(scheduler, 'send_exercise', send)
    for number in (1, 2):
        scheduler.send_scheduled_message(0, '', other.id)
        assert latest_attempt(db, other.id).attempt == number
        assert 'scheduled_delivery_recovery' in jobs
        assert jobs['scheduled_delivery_recovery'].trigger == 'interval'
        assert jobs['scheduled_delivery_recovery'].jobstore == 'delivery_recovery'
        latest_attempt(db, other.id).next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.flush()
        if number == 1:
            transition_current_plan(db, user_id=user.id, operation='pause', source_operation_id='pause-retry')
            jobs.pop('scheduled_delivery_recovery')  # Simulate a fresh alarm after restart.
            scheduler.reconcile_scheduled_deliveries()
            assert remaining[0] == 1
            assert jobs['scheduled_delivery_recovery'].next_run_time >= utc(other.expires_at)
            transition_current_plan(db, user_id=user.id, operation='resume', source_operation_id='resume-retry')
            scheduler.reconcile_plan_schedule(other.day.plan_id)
            jobs.pop('scheduled_delivery_recovery')
            scheduler._enable_delivery_recovery()
            assert utc(other.scheduled_for) <= jobs['scheduled_delivery_recovery'].next_run_time < utc(other.expires_at)
            # Resume moves pending slots into the future. Advance the test clock
            # to that real slot instead of bypassing the eligibility gate.
            moment = utc(other.scheduled_for) + timedelta(seconds=1)
            class ClockDatetime(datetime):
                @classmethod
                def now(cls, tz=None): return moment if tz else moment.replace(tzinfo=None)
            from app import scheduled_delivery
            monkeypatch.setattr(scheduler, 'datetime', ClockDatetime)
            monkeypatch.setattr(scheduled_delivery, 'datetime', ClockDatetime)
    scheduler.reconcile_scheduled_deliveries()
    assert latest_attempt(db, other.id).attempt == 3 and sent_receipt(db, other.id)
    assert db.query(ExerciseDelivery).filter_by(state='retryable').count() == 0
    assert 'scheduled_delivery_recovery' not in jobs


def test_startup_retires_permanent_recovery_interval(monkeypatch):
    from app import scheduler
    added = {}; removed = []
    class FakeScheduler:
        running = True
        def remove_job(self, job_id): removed.append(job_id)
        def add_job(self, func, trigger, **kwargs): added[kwargs['id']] = trigger
    monkeypatch.setattr(scheduler, 'scheduler', FakeScheduler())
    scheduler.init_scheduler()
    assert 'scheduled_delivery_recovery' in removed
    assert 'scheduled_delivery_recovery' not in added


@pytest.mark.parametrize('variant', ['text', 'gif'])
async def test_real_callback_on_paused_plan_accepts_done_and_preserves_deadline(migrated_engine, monkeypatch, variant):
    from sqlalchemy.orm import sessionmaker
    from app import telegram, scheduler, exercise_status
    with Session(migrated_engine) as db:
        user, _, step = active_step(db)
        claim = deliver(db, step, variant)
        deadline = step.expires_at
        transition_current_plan(db, user_id=user.id, operation='pause', source_operation_id='pause')
        db.commit(); user_id, tg_id, step_id = user.id, user.tg_id, step.id
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False)
    monkeypatch.setattr(telegram, 'SessionLocal', factory)
    monkeypatch.setattr(scheduler, 'SessionLocal', factory)
    class FakeBot:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        edit_message_text = AsyncMock()
        edit_message_caption = AsyncMock()
    monkeypatch.setattr(exercise_status, 'Bot', FakeBot)
    callback = SimpleNamespace(id='paused-done', data=f'task_complete:{step_id}',
                               from_user=SimpleNamespace(id=tg_id), message=SimpleNamespace(answer=AsyncMock()), answer=AsyncMock())
    try:
        await telegram.handle_task_completed(callback)
        await telegram.handle_task_completed(callback)
        callback.answer.assert_awaited_with('Виконано')
        callback.message.answer.assert_not_awaited()
        edit = FakeBot.edit_message_caption if variant == 'gif' else FakeBot.edit_message_text
        edit.assert_awaited_once()
        assert 'Виконано' in edit.call_args.kwargs.get('text', edit.call_args.kwargs.get('caption'))
        assert edit.call_args.kwargs['reply_markup'] is not None
        with Session(migrated_engine) as db:
            step = db.get(AIPlanStep, step_id)
            assert step.day.plan.status == 'paused' and step.step_status == 'completed'
            assert step.expires_at == deadline
            assert sum(e.event_name == 'task_completed' for e in events(db, step)) == 1
            assert sent_receipt(db, step_id).presentation_snapshot == claim.presentation.to_payload()
    finally:
        with Session(migrated_engine) as db:
            db.execute(text('TRUNCATE users CASCADE')); db.commit()


@pytest.mark.parametrize('reason', ['missing', 'forbidden', 'snapshot'])
def test_permanent_projection_is_diagnostic_not_an_infinite_repair(db, monkeypatch, reason):
    from app import scheduler, exercise_status
    from app.lifecycle import transition_plan_step
    from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
    from aiogram.methods import EditMessageText
    user, _, step = active_step(db)
    if reason == 'snapshot':
        transition_plan_step(db, user_id=user.id, step_id=step.id, target_status='delivered', source_operation_id='manual-import')
        db.add(ExerciseDelivery(plan_step_id=step.id, source_operation_id=f'scheduler:delivery:{step.id}', attempt=1,
                               state='delivered', presentation_snapshot={}, chat_id=user.tg_id, message_id=777,
                               variant='text', rendered_payload='imported', started_at=datetime.now(timezone.utc), confirmed_at=datetime.now(timezone.utc)))
        db.flush()
    else:
        deliver(db, step)
    action(db, user, step, 'completed')
    method = EditMessageText(chat_id=user.tg_id, message_id=777, text='status')
    error = TelegramForbiddenError(method=method, message='bot was blocked by the user') if reason == 'forbidden' else TelegramBadRequest(method=method, message='message to edit not found')
    edit = AsyncMock(side_effect=error)
    class FakeBot:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        edit_message_text = edit
    monkeypatch.setattr(exercise_status, 'Bot', FakeBot)
    monkeypatch.setattr(db, 'commit', db.flush)
    assert exercise_status.reconcile_status(db, step.id) is False
    receipt = sent_receipt(db, step.id)
    assert receipt.projection_failure_code == {'missing':'message_uneditable', 'forbidden':'chat_inaccessible', 'snapshot':'invalid_snapshot'}[reason]
    assert receipt.visible_status is None and receipt.state == 'delivered'
    assert not exercise_status.pending_status_step_ids(db)
    monkeypatch.setattr(scheduler, 'SessionLocal', lambda: nullcontext(db))
    monkeypatch.setattr(scheduler, '_enable_delivery_recovery', lambda: None)
    scheduler.reconcile_scheduled_deliveries()
    assert edit.await_count == (0 if reason == 'snapshot' else 1)
    if reason != 'snapshot':
        # Explicit user activity after access restoration may retry once.
        edit.side_effect = None
        assert exercise_status.reconcile_status(db, step.id) is True
        assert receipt.projection_failure_code is None and receipt.visible_status == 'completed'


def test_due_backlog_dispatches_with_bounded_concurrency(db, monkeypatch):
    from app import scheduler
    from app.db import AIPlanDay
    user, plan, _ = active_step(db)
    steps = db.query(AIPlanStep).join(AIPlanDay).filter(AIPlanDay.plan_id == plan.id).all()
    for step in steps:
        step.scheduled_for = datetime.now(timezone.utc)-timedelta(minutes=1)
        step.expires_at = datetime.now(timezone.utc)+timedelta(hours=1)
    db.flush()
    barrier = Barrier(len(steps)); errors = []; called = []
    def send(_chat, _text, step_id):
        called.append(step_id)
        try: barrier.wait(timeout=3)
        except Exception as exc: errors.append(exc)
    monkeypatch.setattr(scheduler, 'SessionLocal', lambda: nullcontext(db))
    monkeypatch.setattr(scheduler, 'send_scheduled_message', send)
    monkeypatch.setattr(scheduler, '_enable_delivery_recovery', lambda: None)
    scheduler.reconcile_scheduled_deliveries()
    assert set(called) == {step.id for step in steps} and not errors


@pytest.mark.parametrize('operation', ['completed', 'skipped', 'feedback'])
async def test_real_callback_lock_wait_does_not_block_polling_or_lose_arrival_time(migrated_engine, monkeypatch, operation):
    from threading import Event, Thread, Timer
    from time import monotonic
    from sqlalchemy.orm import sessionmaker
    from app import telegram, scheduler, exercise_status
    with Session(migrated_engine) as db:
        user, _, step = active_step(db)
        step.expires_at = datetime.now(timezone.utc)+timedelta(seconds=.3)
        db.flush(); deliver(db, step)
        if operation == 'feedback': action(db, user, step, 'completed')
        db.commit(); user_id, tg_id, step_id, deadline = user.id, user.tg_id, step.id, step.expires_at
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False)
    monkeypatch.setattr(telegram, 'SessionLocal', factory); monkeypatch.setattr(scheduler, 'SessionLocal', factory)
    class FakeBot:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        edit_message_text = AsyncMock()
    monkeypatch.setattr(exercise_status, 'Bot', FakeBot)
    held = Event(); release = Event()
    def hold():
        with Session(migrated_engine) as db:
            db.query(User).filter_by(id=user_id).with_for_update().one()
            held.set(); release.wait(3); db.commit()
    holder = Thread(target=hold); holder.start(); assert held.wait(3)
    timer = Timer(.6, release.set); timer.start()
    cb = SimpleNamespace(id='waiting-tap', data=f'task_feedback:{step_id}:better' if operation == 'feedback' else f'task_complete:{step_id}',
                         from_user=SimpleNamespace(id=tg_id), message=SimpleNamespace(answer=AsyncMock()), answer=AsyncMock())
    try:
        handler = telegram.handle_task_feedback if operation == 'feedback' else lambda cb: telegram._handle_step_action(cb, operation)
        task = asyncio.create_task(handler(cb))
        started = monotonic(); await asyncio.sleep(.03)
        assert monotonic()-started < .3  # Another polling coroutine remains responsive while the row is locked.
        await task
        with Session(migrated_engine) as db:
            step = db.get(AIPlanStep, step_id)
            assert step.step_status == ('completed' if operation == 'feedback' else operation)
            if operation != 'feedback':
                assert step.terminal_at < deadline < datetime.now(timezone.utc)
                event = next(e for e in events(db, step) if e.event_name == 'task_'+operation)
                assert event.occurred_at == step.terminal_at
            else: assert db.query(FeedbackEvent).filter_by(plan_step_id=step_id).one().value == 'better'
    finally:
        release.set(); holder.join(3); timer.cancel()
        with Session(migrated_engine) as db: db.execute(text('TRUNCATE users CASCADE')); db.commit()

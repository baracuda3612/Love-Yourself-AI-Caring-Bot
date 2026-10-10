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

"""WP-03.5 rehearsal: each module creates/drops a fresh migrated database."""
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import psycopg2
from psycopg2 import sql
import pytest
from sqlalchemy import create_engine, func, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.content_library import load_content_library
from app.db import AIPlan, AIPlanDay, AIPlanStep, PlanCompletion, User, UserProfile
from app.lifecycle import (complete_current_plan_if_ready, transition_plan_step,
    abandon_current_plan, transition_current_plan, change_delivery_time, switch_plan_format)
from app.plan_drafts.service import create_plan_for_lifecycle
from app.plan_completion.workflow import (claim_report, finish_report, blocked_successor,
    refresh_report_time, UNCERTAIN_TTL, SEND_TIMEOUT)
from app.plan_completion.chronology import utc
from scripts.test_migrations import _run_alembic

pytestmark = pytest.mark.skipif(os.environ.get('WP03_5_POSTGRES_REHEARSAL') != '1', reason='explicit fresh disposable PostgreSQL rehearsal')


@pytest.fixture(scope='module')
def pg_engine():
    admin_url = os.environ['WP03_5_TEST_ADMIN_URL']
    name = 'ly_wp035_' + uuid4().hex
    url = admin_url.rsplit('/', 1)[0] + '/' + name
    admin = psycopg2.connect(admin_url); admin.autocommit = True
    with admin.cursor() as c:
        c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    engine = None
    try:
        _run_alembic(url, 'head')
        engine = create_engine(url)
        with Session(engine) as db:
            load_content_library(db); db.commit()
        yield engine
    finally:
        if engine: engine.dispose()
        with admin.cursor() as c:
            c.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s AND pid<>pg_backend_pid()', (name,))
            c.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
        admin.close()


@pytest.fixture
def db(pg_engine):
    connection = pg_engine.connect(); tx = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close(); tx.rollback(); connection.close()


def source_plan(db, *, medium=False, zone='UTC', work_days=None):
    user = User(tg_id=990350000 + uuid4().int % 10000000, is_active=True, timezone=zone)
    db.add(user); db.flush()
    from tests.access_fixtures import enroll
    enroll(db, user)
    db.add(UserProfile(user_id=user.id, daily_time_slots={'DAY':'14:00','EVENING':'20:30'},
        evening_slot_collected=medium, active_days=work_days or ['MON','TUE','WED','THU','FRI']))
    db.flush()
    # The first plan remains SHORT. MEDIUM source represents an explicitly
    # selected later cycle; use the same target builder after a historical row.
    first = create_plan_for_lifecycle(db, user.id, 'SHORT', source_operation_id='seed:first', activation_receipt_status='active').plan
    if medium:
        abandon_current_plan(db, user_id=user.id, source_operation_id='seed:abandon')
        first = create_plan_for_lifecycle(db, user.id, 'MEDIUM', source_operation_id='seed:medium', activation_receipt_status='active').plan
    return user, first


def terminalize(db, user, plan, *, status='completed', at=None, leave=0):
    at = at or datetime.now(timezone.utc)
    steps = db.query(AIPlanStep).join(AIPlanDay).filter(AIPlanDay.plan_id == plan.id).order_by(AIPlanStep.id).all()
    for step in steps[:len(steps)-leave]:
        # Seed actual scheduled dates before the synthetic terminal instant.
        step.scheduled_for = at-timedelta(hours=3)
        step.expires_at = at if status == 'expired' else at+timedelta(hours=3)
        if status in ('completed','skipped'):
            transition_plan_step(db,user_id=user.id,step_id=step.id,target_status='delivered',source_operation_id=f'seed:sent:{step.id}',occurred_at=at-timedelta(minutes=1))
        transition_plan_step(db,user_id=user.id,step_id=step.id,target_status=status,source_operation_id=f'seed:{status}:{step.id}',occurred_at=at)
    db.flush()
    return steps


def close(db, user, plan, *, now=None):
    return complete_current_plan_if_ready(db,user_id=user.id,plan_id=plan.id,source_operation_id=f'runtime:complete:{plan.id}',now=now)


def first_step(db, plan_id):
    return db.query(AIPlanStep).join(AIPlanDay).filter(AIPlanDay.plan_id == plan_id).order_by(AIPlanStep.scheduled_for, AIPlanStep.id).first()


@pytest.mark.parametrize('slots,expected_time', [
    (None, '14:00'), ({}, '14:00'), ({'EVENING': '20:30'}, '14:00'),
    ({'DAY': '16:45'}, '16:45'),
])
@pytest.mark.parametrize('action', ['completed', 'skipped', 'expired'])
def test_nullable_saved_slots_completion_and_report(db, slots, expected_time, action):
    user, plan = source_plan(db, work_days=['MON', 'FRI'])
    now = datetime(2026, 11, 6, 23, 59, 59, tzinfo=timezone.utc)
    terminalize(db, user, plan, status=action, at=now)
    user.profile.daily_time_slots = slots
    db.flush()
    if slots is None:
        # SQL NULL is distinct from a JSON null in this JSON column.
        db.execute(text('UPDATE user_profiles SET daily_time_slots = NULL WHERE user_id = :id'), {'id': user.id})
        db.refresh(user.profile)
    close(db, user, plan, now=now + timedelta(seconds=1))
    row = db.get(PlanCompletion, plan.id)
    first = first_step(db, row.successor_plan_id)
    assert plan.status == 'completed'
    assert first.scheduled_for.strftime('%H:%M') == expected_time
    if action == 'expired':
        assert row.report_at.strftime('%Y-%m-%d %H:%M') == f'2026-11-09 {expected_time}'
        assert first.scheduled_for.date().isoformat() == '2026-11-13'
    assert first.scheduled_for.date() > row.report_at.date()
    claim = claim_report(db, user.id, plan.id, now=row.report_at)
    assert first.scheduled_for.strftime('%d.%m.%Y') in claim.payload
    assert f'о {expected_time}' in claim.payload
    assert close(db, user, plan, now=row.report_at).duplicate
    assert db.query(PlanCompletion).filter_by(plan_id=plan.id).count() == 1
    assert user.profile.daily_time_slots == slots


@pytest.mark.parametrize('slots', [None, {}, {'EVENING': '20:30'}])
@pytest.mark.parametrize('release', [False, True])
def test_nullable_saved_slots_late_report_recovery(db, slots, release):
    user, plan = source_plan(db)
    now = datetime.now(timezone.utc)
    terminalize(db, user, plan, at=now)
    close(db, user, plan, now=now)
    claim = claim_report(db, user.id, plan.id, now=now)
    if release:
        claim_report(db, user.id, plan.id, now=now + SEND_TIMEOUT)
    else:
        finish_report(db, user.id, claim, outcome='retryable', retry_after=60, now=now)
    user.profile.daily_time_slots = slots
    db.flush()
    if slots is None:
        db.execute(text('UPDATE user_profiles SET daily_time_slots = NULL WHERE user_id = :id'), {'id': user.id})
        db.refresh(user.profile)
    late = now + timedelta(days=7)
    recovered = claim_report(db, user.id, plan.id, now=late,
        release_operation_id='nullable:continue' if release else None)
    row = db.get(PlanCompletion, plan.id)
    first = first_step(db, row.successor_plan_id)
    assert recovered.release == release
    assert first.scheduled_for.date() > late.date()
    assert first.scheduled_for.strftime('%H:%M') == '14:00'
    assert first.scheduled_for.strftime('%d.%m.%Y') in recovered.payload
    assert 'о 14:00' in recovered.payload
    assert db.query(blocked_successor(row.successor_plan_id)).scalar()
    finish_report(db, user.id, recovered, outcome='sent', message_id=778, now=late)
    assert not db.query(blocked_successor(row.successor_plan_id)).scalar()
    assert user.profile.daily_time_slots == slots


@pytest.mark.parametrize('collected,slots', [
    (False, {'DAY': '14:00', 'EVENING': '20:30'}),
    (True, None), (True, {}), (True, {'DAY': '14:00'}),
    (True, {'DAY': '14:00', 'EVENING': '99:00'}),
])
def test_nullable_saved_slots_do_not_bypass_medium_gate(db, collected, slots):
    from app.lifecycle import LifecycleTransitionError
    from app.time_slots import TimeSlotError
    user, plan = source_plan(db, medium=True)
    terminalize(db, user, plan)
    user.profile.daily_time_slots = slots
    user.profile.evening_slot_collected = collected
    db.flush()
    with pytest.raises((LifecycleTransitionError, TimeSlotError)), db.begin_nested():
        close(db, user, plan)
    db.refresh(plan)
    assert plan.status == 'active' and db.get(PlanCompletion, plan.id) is None
    assert db.query(AIPlan).filter_by(user_id=user.id).count() == 2


def test_nullable_saved_day_preserves_collected_medium_evening_in_recovery(db):
    user, plan = source_plan(db, medium=True)
    now = datetime.now(timezone.utc)
    terminalize(db, user, plan, at=now)
    user.profile.daily_time_slots = {'EVENING': '20:30'}
    db.flush()
    close(db, user, plan, now=now)
    row = db.get(PlanCompletion, plan.id)
    late = now + timedelta(days=7)
    claim = claim_report(db, user.id, plan.id, now=late)
    successor = db.get(AIPlan, row.successor_plan_id)
    assert successor.total_days == 14
    assert len([step for day in successor.days for step in day.steps]) == 28
    for day in successor.days:
        assert {s.time_slot: s.scheduled_for.strftime('%H:%M') for s in day.steps} == {
            'DAY': '14:00', 'EVENING': '20:30'}
        assert all(s.scheduled_for.date() > late.date() for s in day.steps)
    first = first_step(db, successor.id)
    assert first.scheduled_for.strftime('%d.%m.%Y') in claim.payload
    assert 'о 14:00' in claim.payload


@pytest.mark.parametrize('medium', [False, True])
@pytest.mark.parametrize('action', ['completed','skipped'])
def test_atomic_same_format_explicit_source_and_replay(db, medium, action):
    user, plan = source_plan(db, medium=medium)
    now = datetime.now(timezone.utc)
    terminalize(db,user,plan,status=action,at=now)
    result = close(db,user,plan,now=now)
    row = db.get(PlanCompletion,plan.id)
    successor = db.get(AIPlan,row.successor_plan_id)
    assert result.plan_id == plan.id and plan.status == 'completed'
    assert successor.total_days == (14 if medium else 7)
    assert len([s for d in successor.days for s in d.steps]) == (28 if medium else 7)
    assert row.state == 'pending' and row.report_at == now
    assert first_step(db,successor.id).scheduled_for.date() > now.date()
    assert all(s.exercise_id != 'cold_water_face' and s.content_version == 1 for d in successor.days for s in d.steps)
    assert close(db,user,plan,now=now).duplicate
    assert db.query(PlanCompletion).count() == 1
    assert db.query(AIPlan).filter(AIPlan.user_id == user.id, AIPlan.status == 'active').count() == 1


def test_medium_second_exercise_blocks_completion(db):
    user,plan = source_plan(db,medium=True)
    terminalize(db,user,plan,leave=1)
    assert close(db,user,plan) is None
    assert plan.status == 'active' and not db.query(PlanCompletion).first()


def test_builder_error_rolls_back_completion_but_not_action(db,monkeypatch):
    user,plan = source_plan(db); terminalize(db,user,plan)
    def fail(*a,**k): raise ValueError('builder unavailable')
    monkeypatch.setattr('app.plan_drafts.service.create_plan_for_lifecycle', fail)
    with pytest.raises(ValueError), db.begin_nested(): close(db,user,plan)
    db.refresh(plan)
    assert plan.status == 'active' and not db.query(PlanCompletion).first()
    assert all(s.step_status == 'completed' for d in plan.days for s in d.steps)


@pytest.mark.parametrize('zone', ['Europe/Kyiv','America/New_York','Asia/Kolkata'])
def test_expiry_report_saved_day_then_distinct_next_workday(db,zone):
    import pytz
    user,plan = source_plan(db,zone=zone,work_days=['MON','FRI'])
    # Future Friday expiry, after the real builder activation date.
    tz = pytz.timezone(zone)
    expiry = tz.localize(datetime(2026,11,6,23,59,59)).astimezone(timezone.utc)
    terminalize(db,user,plan,status='expired',at=expiry)
    close(db,user,plan,now=expiry+timedelta(seconds=1))
    row=db.get(PlanCompletion,plan.id)
    assert utc(row.report_at).astimezone(tz).strftime('%Y-%m-%d %H:%M') == '2026-11-09 14:00'
    first=first_step(db,row.successor_plan_id)
    assert utc(first.scheduled_for).astimezone(tz).strftime('%Y-%m-%d %H:%M') == '2026-11-13 14:00'
    assert claim_report(db,user.id,plan.id,now=expiry+timedelta(minutes=1)) is None


def test_claim_is_durable_unknown_never_resends_and_ttl_user_release(db):
    user,plan=source_plan(db); now=datetime.now(timezone.utc)
    terminalize(db,user,plan,at=now); close(db,user,plan,now=now)
    claim=claim_report(db,user.id,plan.id,now=now)
    assert claim and claim_report(db,user.id,plan.id,now=now) is None
    assert claim_report(db,user.id,plan.id,now=now+SEND_TIMEOUT) is None
    row=db.get(PlanCompletion,plan.id)
    assert row.state=='uncertain'
    assert claim_report(db,user.id,plan.id,now=now+timedelta(days=2)) is None
    assert db.query(blocked_successor(row.successor_plan_id)).scalar()
    assert claim_report(db,user.id,plan.id,now=now+timedelta(minutes=9),release_operation_id='release:1') is None
    release=claim_report(db,user.id,plan.id,now=now+UNCERTAIN_TTL,release_operation_id='release:1')
    assert release.release and release.payload != claim.payload
    assert 'Повторно його не надсилаємо' in release.payload
    finish_report(db,user.id,release,outcome='sent',message_id=998,now=now+UNCERTAIN_TTL)
    assert row.state=='released' and row.release_message_id==998
    assert not db.query(blocked_successor(row.successor_plan_id)).scalar()
    assert claim_report(db,user.id,plan.id,now=now+timedelta(days=2),release_operation_id='release:1') is None


def test_known_failure_retry_reanchors_after_late_report(db):
    user,plan=source_plan(db); now=datetime.now(timezone.utc)
    terminalize(db,user,plan,at=now); close(db,user,plan,now=now)
    claim=claim_report(db,user.id,plan.id,now=now)
    finish_report(db,user.id,claim,outcome='retryable',retry_after=60,now=now)
    row=db.get(PlanCompletion,plan.id)
    assert claim_report(db,user.id,plan.id,now=now+timedelta(seconds=59)) is None
    late=now+timedelta(days=7)
    again=claim_report(db,user.id,plan.id,now=late)
    first=first_step(db,row.successor_plan_id)
    assert first.scheduled_for.date()>late.date()
    assert first.scheduled_for.strftime('%d.%m.%Y') in again.payload
    finish_report(db,user.id,again,outcome='sent',message_id=77,now=late)
    assert row.state=='sent' and row.schedule_pending
    assert claim_report(db,user.id,plan.id,now=late) is None
    with pytest.raises(DBAPIError), db.begin_nested():
        row.message_id=99; db.flush()


@pytest.mark.parametrize('control',['pause','cancel','time','switch'])
def test_controls_survive_completion_replay_without_false_promise(db,control):
    user,plan=source_plan(db); now=datetime.now(timezone.utc)
    terminalize(db,user,plan,at=now); close(db,user,plan,now=now)
    row=db.get(PlanCompletion,plan.id); successor=db.get(AIPlan,row.successor_plan_id)
    if control=='pause': transition_current_plan(db,user_id=user.id,operation='pause',source_operation_id='control')
    elif control=='cancel': abandon_current_plan(db,user_id=user.id,source_operation_id='control')
    elif control=='time': change_delivery_time(db,user_id=user.id,slot='DAY',hhmm='16:30',source_operation_id='control')
    else:
        user.profile.evening_slot_collected=True; db.flush()
        switch_plan_format(db,user_id=user.id,target_plan_type='MEDIUM',source_operation_id='control')
    assert close(db,user,plan).duplicate
    claim=claim_report(db,user.id,plan.id,now=now)
    if control=='pause': assert 'на паузі' in claim.payload and 'Перша вправа прийде' not in claim.payload
    elif control in ('cancel','switch'): assert 'замінено' in claim.payload and 'Перша вправа прийде' not in claim.payload
    else: assert '16:30' in claim.payload
    assert successor.status == ('paused' if control=='pause' else 'abandoned' if control in ('cancel','switch') else 'active')


def test_no_action_time_change_moves_unattempted_report(db):
    user,plan=source_plan(db)
    expiry=datetime(2026,11,6,23,59,59,tzinfo=timezone.utc)
    terminalize(db,user,plan,status='expired',at=expiry); close(db,user,plan,now=expiry+timedelta(seconds=1))
    row=db.get(PlanCompletion,plan.id)
    change_delivery_time(db,user_id=user.id,slot='DAY',hhmm='09:15',source_operation_id='control')
    refresh_report_time(db,user,row.successor_plan_id); db.flush()
    assert row.report_at.hour==9 and row.report_at.minute==15


def test_recovery_only_selects_unfinished_sources_and_parks(db,monkeypatch):
    from app import scheduler
    user,plan=source_plan(db); terminalize(db,user,plan)
    assert scheduler._terminal_completion_sources(db).all()==[(user.id,plan.id)]
    close(db,user,plan)
    assert scheduler._terminal_completion_sources(db).all()==[]
    row=db.get(PlanCompletion,plan.id)
    @contextmanager
    def retained(): yield db
    monkeypatch.setattr(scheduler,'SessionLocal',retained)
    monkeypatch.setattr(scheduler,'_completion_source_failure',False)
    assert scheduler._completion_recovery_time(datetime.now(timezone.utc)) is not None
    claim=claim_report(db,user.id,plan.id)
    finish_report(db,user.id,claim,outcome='uncertain')
    assert scheduler._completion_recovery_time(datetime.now(timezone.utc)) is None


@pytest.mark.parametrize('state',['pending','in_flight','schedule_pending'])
def test_inactive_completion_parks_but_quarantines_inflight_and_recovers_when_active(db,completion_jobs,monkeypatch,state):
    import asyncio
    from concurrent.futures import Future
    from app import orchestrator
    scheduler,jobs=completion_jobs
    user,plan=source_plan(db);terminalize(db,user,plan);close(db,user,plan)
    row=db.get(PlanCompletion,plan.id)
    if state!='pending':
        claim=claim_report(db,user.id,plan.id)
        if state=='schedule_pending':finish_report(db,user.id,claim,outcome='sent',message_id=77)
        else:
            row.started_at=datetime.now(timezone.utc)-SEND_TIMEOUT-timedelta(seconds=1)
            row.wake_at=datetime.now(timezone.utc)-timedelta(seconds=1)
    user.is_active=False;db.flush()
    calls=[]
    async def dispatch(user_id,plan_id):
        calls.append((user_id,plan_id))
        result=claim_report(db,user_id,plan_id);db.flush()
        return result
    def submit(coro):
        future=Future();future.set_result(asyncio.run(coro));return future
    monkeypatch.setattr(orchestrator,'send_plan_completion_message',dispatch)
    monkeypatch.setattr(scheduler,'_submit_coroutine',submit)
    assert (scheduler._completion_recovery_time(datetime.now(timezone.utc)) is not None)==(state=='in_flight')
    scheduler.reconcile_plan_completions()
    assert len(calls)==(1 if state=='in_flight' else 0) and not jobs
    assert scheduler._completion_recovery_time(datetime.now(timezone.utc)) is None
    scheduler.reconcile_plan_completions()
    assert len(calls)==(1 if state=='in_flight' else 0)
    if state=='in_flight':
        assert row.state=='uncertain' and row.sent_at is None
    else:
        user.is_active=True;db.flush()
        assert scheduler._completion_recovery_time(datetime.now(timezone.utc)) is not None
        scheduler.reconcile_plan_completions()
        if state=='pending':assert calls==[(user.id,plan.id)] and row.state=='in_flight'
        else:
            assert jobs and not row.schedule_pending
            assert len(activation_facts(db,row.successor_plan_id))==1
            scheduler.reconcile_plan_completions()
            assert len(activation_facts(db,row.successor_plan_id))==1


def test_late_confirmation_preserves_receipt_but_requires_future_reconfirmation(db):
    user,plan=source_plan(db); now=datetime.now(timezone.utc)
    terminalize(db,user,plan,at=now); close(db,user,plan,now=now)
    claim=claim_report(db,user.id,plan.id,now=now)
    row=db.get(PlanCompletion,plan.id)
    late=utc(first_step(db,row.successor_plan_id).scheduled_for)+timedelta(minutes=1)
    finish_report(db,user.id,claim,outcome='sent',message_id=77,now=late)
    assert row.state=='sent_held' and row.sent_at==late and row.message_id==77
    assert db.query(blocked_successor(row.successor_plan_id)).scalar()
    release=claim_report(db,user.id,plan.id,now=late,release_operation_id='late:release')
    assert 'доставлено із затримкою' in release.payload
    assert first_step(db,row.successor_plan_id).scheduled_for.date()>late.date()
    finish_report(db,user.id,release,outcome='sent',message_id=78,now=late)
    assert row.state=='released' and row.payload==claim.payload and row.message_id==77


def test_two_database_workers_finalize_and_claim_once(pg_engine):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    with Session(pg_engine) as db:
        user,plan=source_plan(db); terminalize(db,user,plan)
        user_id,plan_id=user.id,plan.id
        db.commit()
    barrier=Barrier(2)
    def complete(_):
        with Session(pg_engine) as db:
            barrier.wait(timeout=10)
            result=complete_current_plan_if_ready(db,user_id=user_id,plan_id=plan_id,source_operation_id=f'runtime:complete:{plan_id}')
            db.commit(); return result.duplicate
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(complete,range(2)))==[False,True]
    barrier=Barrier(2)
    def claim(_):
        with Session(pg_engine) as db:
            barrier.wait(timeout=10)
            result=claim_report(db,user_id,plan_id)
            db.commit(); return result is not None
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim,range(2)))==[False,True]
    with Session(pg_engine) as db:
        row=db.get(PlanCompletion,plan_id)
        assert row.state=='in_flight' and row.attempts==1
        assert db.query(AIPlan).filter(AIPlan.user_id==user_id).count()==2
        claim_report(db,user_id,plan_id,now=datetime.now(timezone.utc)+SEND_TIMEOUT)
        db.commit()  # leave no automatic work from this standalone race fixture


def test_confirmed_summary_releases_exact_successor_jobs_and_parks_recovery(db,monkeypatch):
    from app import scheduler
    user,plan=source_plan(db); terminalize(db,user,plan); close(db,user,plan)
    claim=claim_report(db,user.id,plan.id)
    finish_report(db,user.id,claim,outcome='sent',message_id=77)
    row=db.get(PlanCompletion,plan.id)
    @contextmanager
    def retained(): yield db
    jobs={}
    fake=SimpleNamespace(get_job=lambda job_id,**kw:jobs.get(job_id),
        remove_job=lambda job_id,**kw:jobs.pop(job_id,None),
        modify_job=lambda *a,**k:None,add_job=lambda *a,**k:None)
    def schedule(step,user):
        jobs[scheduler._generate_step_job_id(step)]=SimpleNamespace(next_run_time=step.scheduled_for)
        return True
    monkeypatch.setattr(scheduler,'SessionLocal',retained)
    monkeypatch.setattr(scheduler,'scheduler',fake)
    monkeypatch.setattr(scheduler,'schedule_plan_step',schedule)
    monkeypatch.setattr(scheduler,'_completion_source_failure',False)
    scheduler.reconcile_plan_completions()
    assert len(jobs)==7 and not row.schedule_pending
    assert scheduler._completion_recovery_time(datetime.now(timezone.utc)) is None
    from app.db import UserEvent
    assert db.query(UserEvent).filter(UserEvent.event_name=='plan_completion_sent',UserEvent.plan_id==plan.id).count()==1
    assert db.query(UserEvent).filter(UserEvent.event_name=='plan_activated',UserEvent.plan_id==row.successor_plan_id).count()==1


@pytest.fixture
def completion_jobs(db, monkeypatch):
    from app import db as database, scheduler
    @contextmanager
    def retained(): yield db
    jobs = {}
    fake = SimpleNamespace(get_job=lambda job_id, **kw: jobs.get(job_id),
        remove_job=lambda job_id, **kw: jobs.pop(job_id, None),
        modify_job=lambda *a, **k: None, add_job=lambda *a, **k: None)
    def schedule(step, user):
        jobs[scheduler._generate_step_job_id(step)] = SimpleNamespace(next_run_time=step.scheduled_for)
        return True
    monkeypatch.setattr(database, 'SessionLocal', retained)
    monkeypatch.setattr(scheduler, 'SessionLocal', retained)
    monkeypatch.setattr(scheduler, 'scheduler', fake)
    monkeypatch.setattr(scheduler, 'schedule_plan_step', schedule)
    monkeypatch.setattr(scheduler, '_completion_source_failure', False)
    return scheduler, jobs


def activation_facts(db, plan_id):
    from app.db import AggregateRecord, UserEvent
    events = db.query(UserEvent).filter_by(event_name='plan_activated', plan_id=plan_id).all()
    contributions = db.query(AggregateRecord).filter_by(record_kind='contribution',
        metric_name='plan_activated', source_operation_id=f'plan-activation:{plan_id}').all()
    assert len(events) == len(contributions)
    return events


@pytest.mark.parametrize('control', ['paused', 'cancelled', 'switched', 'inactive'])
def test_completion_activation_excludes_unstarted_successors(db, completion_jobs, control):
    from app.lifecycle_reconciliation import _record_activation_event
    from app.lifecycle import LifecycleResult
    scheduler, jobs = completion_jobs
    user, plan = source_plan(db)
    terminalize(db, user, plan)
    if control == 'paused':
        transition_current_plan(db, user_id=user.id, operation='pause', source_operation_id='event:pause')
    close(db, user, plan)
    row = db.get(PlanCompletion, plan.id)
    successor = db.get(AIPlan, row.successor_plan_id)
    if control == 'cancelled':
        abandon_current_plan(db, user_id=user.id, source_operation_id='event:cancel')
    elif control == 'switched':
        user.profile.evening_slot_collected = True
        db.flush()
        switch_plan_format(db, user_id=user.id, target_plan_type='MEDIUM', source_operation_id='event:switch')
    claim = claim_report(db, user.id, plan.id)
    finish_report(db, user.id, claim, outcome='sent', message_id=77)
    if control == 'inactive':
        user.is_active = False
        db.flush()
    scheduler.reconcile_plan_completions()
    assert not jobs
    assert row.schedule_pending == (control == 'inactive')
    assert activation_facts(db, successor.id) == []
    # Recovery of the builder's activation receipt cannot bypass this gate.
    _record_activation_event(LifecycleResult(user_id=user.id, plan_id=successor.id,
        status=successor.status, operation='activate', details={'total_days': 7}))
    assert activation_facts(db, successor.id) == []


@pytest.mark.parametrize('report_before_resume', [False, True])
def test_completion_activation_on_first_resume_is_once_and_after_report(db, completion_jobs, report_before_resume):
    from app.lifecycle_reconciliation import reconcile_scheduler_effects
    scheduler, jobs = completion_jobs
    user, plan = source_plan(db)
    terminalize(db, user, plan)
    transition_current_plan(db, user_id=user.id, operation='pause', source_operation_id='event:source-pause')
    close(db, user, plan)
    row = db.get(PlanCompletion, plan.id)
    if report_before_resume:
        claim = claim_report(db, user.id, plan.id)
        finish_report(db, user.id, claim, outcome='sent', message_id=77)
        scheduler.reconcile_plan_completions()
    assert not jobs and activation_facts(db, row.successor_plan_id) == []
    before = datetime.now(timezone.utc)
    result = transition_current_plan(db, user_id=user.id, operation='resume', source_operation_id='event:first-resume')
    assert reconcile_scheduler_effects(result).external_effects_succeeded
    if not report_before_resume:
        assert not jobs and activation_facts(db, row.successor_plan_id) == []
        claim = claim_report(db, user.id, plan.id)
        finish_report(db, user.id, claim, outcome='sent', message_id=77)
        scheduler.reconcile_plan_completions()
    facts = activation_facts(db, row.successor_plan_id)
    assert len(jobs) == 7 and len(facts) == 1 and facts[0].occurred_at >= before
    original_occurrence = facts[0].occurred_at
    for operation, key in [('pause', 'event:again-pause'), ('resume', 'event:again-resume')]:
        result = transition_current_plan(db, user_id=user.id, operation=operation, source_operation_id=key)
        assert reconcile_scheduler_effects(result).external_effects_succeeded
    scheduler.reconcile_plan_completions()
    facts = activation_facts(db, row.successor_plan_id)
    assert len(jobs) == 7 and len(facts) == 1 and facts[0].occurred_at == original_occurrence


@pytest.mark.parametrize('failure', ['jobs', 'event'])
@pytest.mark.parametrize('automatic_recovery', [False, True])
def test_completion_activation_resume_failure_replays_without_false_fact(db, completion_jobs, monkeypatch, failure, automatic_recovery):
    from app import telemetry
    from app.lifecycle_reconciliation import reconcile_scheduler_effects
    scheduler, jobs = completion_jobs
    user, plan = source_plan(db)
    terminalize(db, user, plan)
    transition_current_plan(db, user_id=user.id, operation='pause', source_operation_id='event:source-pause')
    close(db, user, plan)
    row = db.get(PlanCompletion, plan.id)
    claim = claim_report(db, user.id, plan.id)
    finish_report(db, user.id, claim, outcome='sent', message_id=77)
    scheduler.reconcile_plan_completions()
    target, name = (scheduler, 'schedule_plan_step') if failure == 'jobs' else (telemetry, 'write_event_operation')
    original = getattr(target, name)
    def fail(*a, **kw): raise RuntimeError('event rehearsal failure')
    monkeypatch.setattr(target, name, fail)
    result = transition_current_plan(db, user_id=user.id, operation='resume', source_operation_id='event:retry-resume')
    assert not reconcile_scheduler_effects(result).external_effects_succeeded
    assert activation_facts(db, row.successor_plan_id) == []
    assert row.schedule_pending
    assert scheduler._completion_recovery_time(datetime.now(timezone.utc)) is not None
    monkeypatch.setattr(target, name, original)
    if automatic_recovery:
        # No user action: the persisted resume obligation survives loss of the
        # caller/result and is sufficient for the ordinary recovery scanner.
        retry_at = utc(row.wake_at) + timedelta(seconds=1)
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None): return retry_at
        monkeypatch.setattr(scheduler, 'datetime', Clock)
        scheduler.reconcile_plan_completions()
        assert not row.schedule_pending
        assert scheduler._completion_recovery_time(datetime.now(timezone.utc)) is None
    else:
        result = transition_current_plan(db, user_id=user.id, operation='resume', source_operation_id='event:retry-resume')
        assert result.duplicate and reconcile_scheduler_effects(result).external_effects_succeeded
    assert len(jobs) == 7 and len(activation_facts(db, row.successor_plan_id)) == 1


def test_completion_activation_persistent_failure_backs_off_and_isolates(db, completion_jobs, monkeypatch):
    from app import telemetry
    scheduler, jobs = completion_jobs
    rows = []
    for _ in range(2):
        user, plan = source_plan(db)
        terminalize(db, user, plan)
        close(db, user, plan)
        claim = claim_report(db, user.id, plan.id)
        finish_report(db, user.id, claim, outcome='sent', message_id=77)
        rows.append(db.get(PlanCompletion, plan.id))
    broken, healthy = rows
    original = telemetry.write_event_operation
    calls = []
    def write(*a, **kw):
        if kw.get('source_operation_id') == f'plan-activation:{broken.successor_plan_id}':
            calls.append(kw['source_operation_id'])
            raise RuntimeError('persistent telemetry failure')
        return original(*a, **kw)
    monkeypatch.setattr(telemetry, 'write_event_operation', write)
    moment = SimpleNamespace(at=datetime.now(timezone.utc))
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return moment.at
    monkeypatch.setattr(scheduler, 'datetime', Clock)
    for attempt in range(2):
        scheduler.reconcile_plan_completions()
        assert len(calls) == attempt + 1 and broken.schedule_pending
        assert utc(broken.wake_at) >= moment.at + timedelta(seconds=30)
        assert scheduler._completion_recovery_time(moment.at) >= moment.at + timedelta(seconds=30)
        scheduler.reconcile_plan_completions()  # no due retry at the same instant
        assert len(calls) == attempt + 1
        moment.at = utc(broken.wake_at) + timedelta(seconds=1)
    assert activation_facts(db, broken.successor_plan_id) == []
    assert len(activation_facts(db, healthy.successor_plan_id)) == 1 and not healthy.schedule_pending
    monkeypatch.setattr(telemetry, 'write_event_operation', original)
    scheduler.reconcile_plan_completions()
    assert not broken.schedule_pending
    assert len(activation_facts(db, broken.successor_plan_id)) == 1
    assert scheduler._completion_recovery_time(moment.at) is None


@pytest.mark.parametrize('target',['completed','skipped'])
def test_final_delivered_action_while_paused_closes_and_carries_pause(db,target):
    from app.lifecycle import transition_owned_plan_step
    from app import scheduler
    user,plan=source_plan(db); now=datetime.now(timezone.utc)
    steps=terminalize(db,user,plan,at=now,leave=1)
    last=steps[-1]; last.scheduled_for=now-timedelta(hours=1); last.expires_at=now+timedelta(hours=1)
    transition_plan_step(db,user_id=user.id,step_id=last.id,target_status='delivered',source_operation_id='paused:last-delivery',occurred_at=now-timedelta(minutes=1))
    transition_current_plan(db,user_id=user.id,operation='pause',source_operation_id='paused:control')
    transition_owned_plan_step(db,telegram_user_id=user.tg_id,step_id=last.id,target_status=target,source_operation_id='paused:last-action',occurred_at=now)
    assert scheduler._terminal_completion_sources(db).all()==[(user.id,plan.id)]
    close(db,user,plan,now=now)
    row=db.get(PlanCompletion,plan.id); successor=db.get(AIPlan,row.successor_plan_id)
    assert plan.status=='completed' and successor.status=='paused' and row.report_at==now
    assert scheduler._terminal_completion_sources(db).all()==[]
    claim=claim_report(db,user.id,plan.id,now=now)
    assert 'більше немає незавершених вправ' in claim.payload
    assert 'на паузі' in claim.payload and 'зніми паузу' in claim.payload
    assert 'Перша вправа прийде' not in claim.payload
    finish_report(db,user.id,claim,outcome='sent',message_id=77,now=now)
    assert not scheduler.can_deliver_tasks(user,successor)
    assert close(db,user,plan,now=now).duplicate


def test_paused_no_action_does_not_create_automatic_paused_cycles(db):
    from app import scheduler
    user,plan=source_plan(db)
    transition_current_plan(db,user_id=user.id,operation='pause',source_operation_id='pause')
    terminalize(db,user,plan,status='expired')
    assert close(db,user,plan) is None
    assert scheduler._terminal_completion_sources(db).all()==[]


@pytest.mark.parametrize('state',['sent','sent_held','released'])
def test_database_rejects_null_message_identity_in_confirmed_states(db,state):
    user,plan=source_plan(db); terminalize(db,user,plan); close(db,user,plan)
    row=db.get(PlanCompletion,plan.id)
    with pytest.raises(DBAPIError), db.begin_nested():
        row.state=state
        row.chat_id=user.tg_id
        row.payload='summary'; row.sent_at=datetime.now(timezone.utc)
        row.release_payload='confirmation'; row.released_at=datetime.now(timezone.utc)
        row.message_id=None; row.release_message_id=None
        db.flush()


def test_delivery_time_into_past_moves_pending_successor_groups_to_next_workday(db,monkeypatch):
    from app import time_slots
    from app.active_days import next_active_date
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None): return datetime(2030,1,4,10,tzinfo=timezone.utc)
    user,plan=source_plan(db); terminalize(db,user,plan); close(db,user,plan)
    row=db.get(PlanCompletion,plan.id); successor=db.get(AIPlan,row.successor_plan_id)
    cursor=datetime(2030,1,4,tzinfo=timezone.utc).date()
    for day in successor.days:
        cursor=next_active_date(cursor,user.profile.active_days)
        for step in day.steps:
            step.scheduled_for=datetime.combine(cursor,datetime.min.time(),tzinfo=timezone.utc)+timedelta(hours=14)
            step.expires_at=step.scheduled_for.replace(hour=23,minute=59,second=59)
        cursor+=timedelta(days=1)
    db.flush()
    monkeypatch.setattr(time_slots,'datetime',Clock)
    change_delivery_time(db,user_id=user.id,slot='DAY',hhmm='07:00',source_operation_id='earlier:day')
    steps=[s for d in successor.days for s in d.steps]
    assert len(steps)==7 and all(s.scheduled_for>Clock.now() for s in steps)
    assert min(s.scheduled_for for s in steps)==datetime(2030,1,7,7,tzinfo=timezone.utc)
    assert len({s.scheduled_for.date() for s in steps})==7
    assert all(s.scheduled_for < s.expires_at for s in steps)


def test_retiming_unattempted_no_action_report_into_past_uses_next_day_and_moves_seam(db,monkeypatch):
    from app.plan_completion import workflow
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None): return datetime(2026,11,9,10,tzinfo=timezone.utc)
    user,plan=source_plan(db)
    expiry=datetime(2026,11,6,23,59,59,tzinfo=timezone.utc)
    terminalize(db,user,plan,status='expired',at=expiry); close(db,user,plan,now=expiry+timedelta(seconds=1))
    row=db.get(PlanCompletion,plan.id)
    assert row.report_at==datetime(2026,11,9,14,tzinfo=timezone.utc)
    user.profile.daily_time_slots={'DAY':'07:00','EVENING':'20:30'};db.flush()
    monkeypatch.setattr(workflow,'datetime',Clock)
    refresh_report_time(db,user,row.successor_plan_id);db.flush()
    assert row.report_at==datetime(2026,11,10,7,tzinfo=timezone.utc)
    # Reconciliation alone must not keep rolling an unchanged preference.
    refresh_report_time(db,user,row.successor_plan_id)
    assert row.report_at==datetime(2026,11,10,7,tzinfo=timezone.utc)
    assert claim_report(db,user.id,plan.id,now=Clock.now()) is None
    claim=claim_report(db,user.id,plan.id,now=row.report_at)
    assert first_step(db,row.successor_plan_id).scheduled_for==datetime(2026,11,11,7,tzinfo=timezone.utc)
    assert '11.11.2026 о 07:00' in claim.payload


@pytest.mark.parametrize('paused',[False,True])
def test_saved_day_atomically_retimes_report_despite_legacy_null_and_paused_effect(db,monkeypatch,paused):
    from app import scheduler, lifecycle_reconciliation
    from app.lifecycle import ExternalEffectState
    user,plan=source_plan(db)
    expiry=datetime(2026,11,6,23,59,59,tzinfo=timezone.utc)
    steps=terminalize(db,user,plan,status='expired',at=expiry)
    db.execute(text("UPDATE ai_plan_steps SET step_status='completed',terminal_at=NULL,version=0 WHERE id=:id"),{'id':steps[0].id})
    db.expire_all()
    close(db,user,plan,now=expiry+timedelta(seconds=1))
    row=db.get(PlanCompletion,plan.id)
    if paused: transition_current_plan(db,user_id=user.id,operation='pause',source_operation_id='pause:before-time')
    result=change_delivery_time(db,user_id=user.id,slot='DAY',hhmm='09:15',source_operation_id='time:summary')
    assert row.report_at==datetime(2026,11,9,9,15,tzinfo=timezone.utc)
    assert row.wake_at==row.report_at
    completion=[effect for effect in result.effects if effect.kind=='reconcile_completion_schedule']
    assert len(completion)==1 and completion[0].state is ExternalEffectState.PENDING
    if paused:
        assert result.effects[0].state is ExternalEffectState.DEFERRED
        calls=[]
        def fail(): raise OSError('alarm unavailable')
        monkeypatch.setattr(scheduler,'enable_completion_recovery',fail)
        failed=lifecycle_reconciliation.reconcile_scheduler_effects(result)
        assert failed.effects[-1].state is ExternalEffectState.FAILED
        # Exact replay must preserve the owed alarm even though exercise jobs
        # are still deferred by pause.
        replay=change_delivery_time(db,user_id=user.id,slot='DAY',hhmm='09:15',source_operation_id='time:summary')
        monkeypatch.setattr(scheduler,'enable_completion_recovery',lambda:calls.append('armed'))
        repaired=lifecycle_reconciliation.reconcile_scheduler_effects(replay)
        assert repaired.effects[-1].state is ExternalEffectState.SUCCEEDED and calls==['armed']
        assert db.get(AIPlan,row.successor_plan_id).status=='paused'


def test_saved_day_and_report_retime_rollback_together(db,monkeypatch):
    from app import lifecycle
    user,plan=source_plan(db)
    expiry=datetime(2026,11,6,23,59,59,tzinfo=timezone.utc)
    terminalize(db,user,plan,status='expired',at=expiry); close(db,user,plan,now=expiry+timedelta(seconds=1))
    row=db.get(PlanCompletion,plan.id)
    def fail(*args,**kwargs): raise RuntimeError('receipt unavailable')
    monkeypatch.setattr(lifecycle,'record_lifecycle_operation',fail)
    with pytest.raises(RuntimeError), db.begin_nested():
        change_delivery_time(db,user_id=user.id,slot='DAY',hhmm='09:15',source_operation_id='time:rollback')
    db.refresh(row); db.refresh(user.profile)
    assert row.report_at==datetime(2026,11,9,14,tzinfo=timezone.utc)
    assert user.profile.daily_time_slots['DAY']=='14:00'

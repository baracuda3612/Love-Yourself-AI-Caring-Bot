"""WP-04.2 only ever creates new disposable PostgreSQL databases."""
import asyncio
import json
import os
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import psycopg2
from psycopg2 import sql
import pytest
from sqlalchemy import create_engine, text, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.exc import DBAPIError
from fastapi.testclient import TestClient

from app import db as m, privacy, access, api
from app.plan_completion import tokens
from app.session_memory import SessionMemory
from app.content_library import load_content_library
from scripts.test_migrations import _run_alembic
from tests.access_fixtures import deployment, enroll
from tests.test_wp_03_5_postgres_completion import source_plan, terminalize, close

pytestmark = pytest.mark.skipif(os.environ.get('WP04_2_POSTGRES_REHEARSAL') != '1', reason='fresh disposable privacy rehearsal only')


@contextmanager
def fresh_database(revision='head'):
    admin_url = os.environ['WP04_2_TEST_ADMIN_URL']
    name = 'ly_wp042_' + uuid4().hex
    url = admin_url.rsplit('/',1)[0] + '/' + name
    admin = psycopg2.connect(admin_url); admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    engine = None
    try:
        _run_alembic(url,revision)
        engine = create_engine(url)
        yield engine, url
    finally:
        if engine: engine.dispose()
        with admin.cursor() as cursor:
            cursor.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s AND pid<>pg_backend_pid()', (name,))
            cursor.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
        admin.close()


@pytest.fixture(scope='module')
def engine():
    with fresh_database() as (engine, _):
        with Session(engine) as db:
            load_content_library(db); db.commit()
        yield engine


@pytest.fixture
def db(engine):
    with engine.connect() as connection:
        transaction = connection.begin()
        with Session(connection, expire_on_commit=False) as db:
            yield db
        transaction.rollback()


def subject(db):
    user = m.User(tg_id=uuid4().int % 10**12, is_active=True)
    db.add(user); db.flush()
    dep = deployment(db)
    dep.support_contact_ref = privacy.SUPPORT_CONTACT
    enroll(db,user,target=dep)
    db.add(m.UserProfile(user_id=user.id,name_preference='friend'))
    notice = privacy.register_notice(db,version=uuid4().hex,notice_text='Disposable exact notice',location='fixture')
    dep.notice_version_id = notice.id
    db.flush()
    return user, dep, notice


def test_exact_notice_receipt_pin_change_and_immutable_content(db):
    user, dep, notice = subject(db)
    with pytest.raises(privacy.NoticeRequired): privacy.pinned_notice(db,user.id,acknowledged=True)
    privacy.acknowledge_notice(db,user.id,notice.id,'telegram:notice:exact')
    privacy.acknowledge_notice(db,user.id,notice.id,'telegram:notice:retry')
    privacy.pinned_notice(db,user.id,acknowledged=True)
    receipt = db.query(m.NoticeAcknowledgement).filter_by(user_id=user.id).one()
    assert receipt.notice_version_id == notice.id and receipt.source_operation_id == 'telegram:notice:exact'
    next_notice = privacy.register_notice(db,version=uuid4().hex,notice_text='Changed text',location='fixture-v2')
    # Publishing another current text never rewrites an accepted deployment.
    privacy.pinned_notice(db,user.id,acknowledged=True)
    dep.notice_version_id = next_notice.id; db.flush()
    with pytest.raises(privacy.NoticeRequired): privacy.pinned_notice(db,user.id,acknowledged=True)
    with pytest.raises(access.AccessDenied): privacy.acknowledge_notice(db,user.id,notice.id,'old-button')
    privacy.acknowledge_notice(db,user.id,next_notice.id,'new-button')
    assert db.query(m.NoticeAcknowledgement).filter_by(user_id=user.id).count() == 2
    with pytest.raises(DBAPIError), db.begin_nested():
        db.execute(text('UPDATE privacy_notice_versions SET notice_text=:body WHERE id=:id'),{'body':'rewrite','id':notice.id})
    next_notice.retired_at = privacy.utcnow(); db.flush()
    with pytest.raises(privacy.NoticeRequired): privacy.pinned_notice(db,user.id)


def test_rights_received_before_verification_and_after_access_revocation(db):
    user, dep, _ = subject(db)
    at = privacy.utcnow()-timedelta(days=4)
    case = privacy.record_manual_request(db,kind='export',received_at=at)
    assert case.due_at == at+timedelta(days=30) and case.verified_at is None
    with pytest.raises(ValueError): privacy.export_request(db,case.id)
    row = privacy.receive_request(db,tg_id=user.tg_id,kind='export',source_operation_id='private:case',case_id=case.id)
    assert row.id == case.id and row.user_id == user.id and row.verified_at
    second, _, _ = subject(db)
    with pytest.raises(access.AccessDenied): privacy.receive_request(db,tg_id=second.tg_id,kind='export',source_operation_id='steal-case',case_id=case.id)
    ent = db.query(m.AccessEntitlement).join(m.DeploymentEnrollment,m.DeploymentEnrollment.entitlement_id==m.AccessEntitlement.id).filter(m.DeploymentEnrollment.user_id==user.id).one()
    ent.revoked_at = privacy.utcnow(); db.flush()
    with pytest.raises(access.AccessDenied): access.require_access(db,user.id)
    deletion = privacy.receive_request(db,tg_id=user.tg_id,kind='deletion',source_operation_id='private:delete')
    assert deletion.owner == privacy.SUPPORT_CONTACT
    with pytest.raises(access.AccessDenied): privacy.receive_request(db,tg_id=-999,kind='access',source_operation_id='spoof')


class Cache:
    def __init__(self): self.data={}; self.ttls={}; self.fail=False
    def set(self,key,value,ex):
        if self.fail: raise ConnectionError('cache down')
        self.data[key]=value; self.ttls[key]=ex
    def delete(self,*keys):
        if self.fail: raise ConnectionError('cache down')
        for key in keys:self.data.pop(key,None)


class AsyncCache:
    def __init__(self,cache):self.cache=cache
    async def delete(self,*keys):return self.cache.delete(*keys)


async def test_retention_cache_partial_stale_missing_outage_and_individual_deadline(engine):
    factory=sessionmaker(engine,expire_on_commit=False)
    now=privacy.utcnow()
    with factory() as db:
        user,_,_=subject(db)
        user_id=user.id
        for i,at in enumerate([now-timedelta(days=91),now-timedelta(days=90),now-timedelta(days=89,hours=23),now]):
            db.add(m.ChatHistory(user_id=user_id,role='user',text=f'body-{i}',created_at=at,expires_at=at+privacy.RETENTION))
        db.commit()
    cache=Cache(); memory=SessionMemory(redis_client=AsyncCache(cache),cache_client=cache,session_factory=factory)
    cache.data[memory._messages_key(user_id)]='partial stale secret'
    cache.data[f'session:{user_id}:messages']='legacy stale secret'
    result=await memory.get_recent_messages(user_id)
    assert [r['text'] for r in result]==['body-2','body-3']
    assert f'session:{user_id}:messages' not in cache.data
    assert 0<cache.ttls[memory._messages_key(user_id)]<=3600
    cache.data.clear()
    assert await memory.get_recent_messages(user_id)==result
    cache.fail=True
    assert await memory.get_recent_messages(user_id)==result
    with factory() as db:
        assert privacy.purge_retained_data(db,batch_size=1,now=now)==1;db.commit()
        assert privacy.purge_retained_data(db,batch_size=1,now=now)==1;db.commit()
        assert privacy.purge_retained_data(db,batch_size=1,now=now)==0
        assert db.query(m.ChatHistory).filter_by(user_id=user_id).count()==2


def test_retention_removes_feedback_copies_and_never_exports_expired_text(db):
    user,_,_=subject(db);now=privacy.utcnow()
    old=m.ChatHistory(user_id=user.id,role='user',text='expired-conversation',created_at=now-timedelta(days=91),expires_at=now-timedelta(days=1))
    current=m.ChatHistory(user_id=user.id,role='assistant',text='retained-conversation',created_at=now,expires_at=now+privacy.RETENTION)
    db.add_all([old,current]);db.flush()
    db.add(m.FeedbackEvent(user_id=user.id,source='product_feedback',source_operation_id='source:feedback',source_message_id=old.id,value='submitted',category='other',extracted_text='expired-copy',context={}))
    db.flush()
    request=privacy.receive_request(db,tg_id=user.tg_id,kind='export',source_operation_id='source:export')
    exported=privacy.export_request(db,request.id)
    # Even before scheduled cleanup, an export cannot recover old source text.
    assert 'expired-conversation' not in json.dumps(exported,default=str)
    assert 'expired-copy' not in json.dumps(exported,default=str)
    privacy.purge_retained_data(db,now=now)
    assert db.query(m.FeedbackEvent).filter_by(user_id=user.id).count()==0
    assert [r['text'] for r in privacy.retained_messages(db,user.id,now=now)]==['retained-conversation']


@pytest.mark.parametrize('case',['valid','expired','revoked','tampered','deleted_user','inactive','entitlement_revoked','cross_purpose','cross_environment','cross_runtime','key_rotation','deployment_changed'])
def test_scoped_grant_current_owner_and_neutral_errors(db,monkeypatch,case):
    user,plan=source_plan(db)
    enrollment=db.query(m.DeploymentEnrollment).filter_by(user_id=user.id,ended_at=None).one()
    raw=tokens.issue_report_grant(db,user.id,plan.id,'completion_report')
    grant=db.query(m.ReportAccessGrant).filter_by(user_id=user.id).one()
    assert raw not in grant.token_digest and str(plan.id)!=raw
    purpose='completion_report'
    if case=='expired':grant.expires_at=privacy.utcnow()-timedelta(seconds=1);grant.issued_at=grant.expires_at-timedelta(days=1)
    elif case=='revoked':tokens.revoke_report_grant(db,grant.id)
    elif case=='tampered':raw=('A' if raw[0]!='A' else 'B')+raw[1:]
    elif case=='inactive':user.is_active=False
    elif case=='entitlement_revoked':db.get(m.AccessEntitlement,enrollment.entitlement_id).revoked_at=privacy.utcnow()
    elif case=='cross_purpose':purpose='pulse_report'
    elif case=='cross_environment':monkeypatch.setattr(access.settings,'ENVIRONMENT','prod')
    elif case=='cross_runtime':monkeypatch.setattr(access.settings,'DEPLOYMENT_ID','other-runtime')
    elif case=='key_rotation':monkeypatch.setattr(access.settings,'REPORT_TOKEN_SECRET','changed-report-secret')
    elif case=='deployment_changed':grant.deployment_id=deployment(db).id
    elif case=='deleted_user':privacy._erase_personal_rows(db,user.id);db.execute(m.User.__table__.delete().where(m.User.id==user.id))
    db.flush()
    if case=='valid':assert tokens.resolve_report_grant(db,raw,purpose)==plan.id
    else:
        with pytest.raises(access.AccessDenied,match='access_unavailable'):tokens.resolve_report_grant(db,raw,purpose)


@pytest.mark.parametrize('kind', ['access', 'export'])
def test_personal_export_excludes_completion_bearers_and_preserves_receipts(db, kind):
    import re
    from app.plan_completion.workflow import claim_report, finish_report
    user,plan=source_plan(db)
    now=privacy.utcnow()
    terminalize(db,user,plan,at=now)
    close(db,user,plan,now=now)
    completion=db.get(m.PlanCompletion,plan.id)
    claim=claim_report(db,user.id,plan.id,now=completion.report_at)
    raw=re.search(r'/report/([A-Za-z0-9_-]{43})',claim.payload).group(1)
    assert tokens.resolve_report_grant(db,raw,'completion_report')==plan.id
    finish_report(db,user.id,claim,outcome='sent',message_id=789,now=completion.report_at)
    original_payload=completion.payload
    request=privacy.receive_request(db,tg_id=user.tg_id,kind=kind,source_operation_id=f'completion:{kind}')
    exported=privacy.export_request(db,request.id)
    serialized=json.dumps(exported,default=str)
    assert raw not in serialized and '/report/' not in serialized
    record=next(r for r in exported['plan_completions'] if r['plan_id']==plan.id)
    assert 'payload' not in record and 'release_payload' not in record
    assert record['state']=='sent' and record['message_id']==789 and record['sent_at']
    assert record['successor_plan_id']==completion.successor_plan_id
    db.expire(completion)
    assert completion.payload==original_payload and completion.message_id==789
    assert tokens.resolve_report_grant(db,raw,'completion_report')==plan.id


def test_manual_correction_requires_verified_case_and_preserves_events(db):
    user,_,_=subject(db)
    request=privacy.receive_request(db,tg_id=user.tg_id,kind='correction',source_operation_id='correction')
    privacy.correct_request(db,request.id,{'name_preference':'new preference'})
    assert db.query(m.UserProfile).filter_by(user_id=user.id).one().name_preference=='new preference'
    with pytest.raises(ValueError):privacy.correct_request(db,request.id,{'tg_id':1})
    privacy.complete_request(db,request.id,evidence_code='correction_confirmed')
    assert request.state=='completed' and request.completed_at


async def test_full_account_erasure_restarts_after_external_failure_and_is_idempotent(engine):
    from app.scheduled_delivery import claim_send
    factory=sessionmaker(engine,expire_on_commit=False)
    with factory() as db:
        user,plan=source_plan(db);now=privacy.utcnow()
        dep=db.query(m.Deployment).join(m.DeploymentEnrollment).filter(m.DeploymentEnrollment.user_id==user.id).one()
        dep.support_contact_ref=privacy.SUPPORT_CONTACT
        notice=privacy.register_notice(db,version=uuid4().hex,notice_text='Delete fixture notice',location='fixture')
        dep.notice_version_id=notice.id;db.flush()
        privacy.acknowledge_notice(db,user.id,notice.id,'delete-fixture:notice')
        first=db.query(m.AIPlanStep).join(m.AIPlanDay).filter(m.AIPlanDay.plan_id==plan.id).order_by(m.AIPlanStep.id).first()
        first.scheduled_for=now-timedelta(seconds=1);first.expires_at=now+timedelta(hours=1);db.flush()
        claim_send(db,first.id)
        terminalize(db,user,plan,at=now);close(db,user,plan,now=now)
        db.add(m.AIPlanVersion(plan_id=plan.id,applied_adaptation_type='legacy_fixture',diff={}))
        history=m.ChatHistory(user_id=user.id,role='user',text='erase me');db.add(history);db.flush()
        db.add(m.FeedbackEvent(user_id=user.id,source='product_feedback',source_operation_id='deletion:feedback',source_message_id=history.id,value='submitted',category='other',extracted_text='erase feedback',context={}))
        instance=m.PlanInstance(user_id=user.id);db.add(instance);db.flush()
        window=m.PlanExecutionWindow(instance_id=instance.id);db.add(window);db.flush()
        db.add(m.FailureSignal(user_id=user.id,plan_execution_id=window.id,step_id='legacy',trigger_event='fixture'))
        db.add(m.TaskStats(user_id=user.id,step_id='legacy'))
        draft=m.PlanDraftRecord(user_id=user.id,duration='SHORT',draft_data={},total_days=7,total_steps=7,is_valid=True)
        db.add(draft);db.flush()
        db.add(m.PlanDraftStep(draft_id=draft.id,day_number=1,exercise_id=first.exercise_id,
            content_version=first.content_version,content_snapshot=first.content_snapshot,time_slot='DAY'))
        enrollment=db.query(m.DeploymentEnrollment).filter_by(user_id=user.id,ended_at=None).one()
        ent=db.get(m.AccessEntitlement,enrollment.entitlement_id)
        identity=db.get(m.AccessIdentity,ent.access_identity_id)
        identity.normalized_email=f'{user.id}@fixture.test'
        roster=m.DeploymentRosterVersion(deployment_id=dep.id,version=1,import_mode='full_snapshot',
            source_ref='disposable',eligible_count=1,invalid_count=0,is_current=False)
        db.add(roster);db.flush()
        db.add(m.DeploymentRosterEntry(roster_version_id=roster.id,access_identity_id=identity.id,
            entry_key='disposable',validation_metadata={}))
        db.add(m.DeploymentInvitation(entitlement_id=ent.id,token_digest=uuid4().hex,token_version=1,
            source_operation_id='delete-fixture:invitation',issued_at=now,expires_at=now+timedelta(minutes=10)))
        db.add(m.AccessProof(deployment_id=dep.id,kind='email',secret_digest=uuid4().hex,
            browser_digest=uuid4().hex,identity_digest=identity.identity_digest,
            issued_at=now,expires_at=now+timedelta(minutes=10)))
        identity_id=identity.id
        raw=tokens.issue_report_grant(db,user.id,plan.id,'completion_report')
        request=privacy.receive_request(db,tg_id=user.tg_id,kind='deletion',source_operation_id='private:delete')
        other,_,_=subject(db)
        user_id,case_id,other_id=user.id,request.id,other.id
        db.execute(text("""INSERT INTO aggregate_records (record_kind,metric_name,metric_schema_version,period_start,period_end,dimension_key,dimensions,numeric_value,sample_count,sealed_at,gate_eligible_count,gate_contributor_count)
        VALUES ('sealed_cell','engagement',1,now()-interval '1 day',now(),ly_aggregate_dimension_key('{"environment":"testnet"}'::jsonb),'{"environment":"testnet"}',1,50,now(),100,50)"""))
        db.commit()
    cache=Cache();memory=SessionMemory(redis_client=AsyncCache(cache),cache_client=cache,session_factory=factory)
    for key in [memory._messages_key(user_id),memory._pending_action_key(user_id),f'session:{user_id}:messages',f'session:{user_id}:pending_action']:cache.data[key]='personal'
    cache.fail=True
    with pytest.raises(ConnectionError):await privacy.delete_account(case_id,memory=memory,cancel_jobs=lambda *a:None,session_factory=factory)
    with factory() as db:
        assert db.get(m.User,user_id).is_active is False
        assert db.get(m.PrivacyRequest,case_id).state=='deleting'
        with pytest.raises(access.AccessDenied):tokens.resolve_report_grant(db,raw,'completion_report')
        with pytest.raises(DBAPIError),db.begin_nested():
            db.execute(text('UPDATE users SET is_active=true WHERE id=:id'),{'id':user_id})
    cache.fail=False
    with pytest.raises(RuntimeError,match='scheduler unavailable'):
        await privacy.delete_account(case_id,memory=memory,cancel_jobs=lambda *a:(_ for _ in ()).throw(RuntimeError('scheduler unavailable')),session_factory=factory)
    await privacy.delete_account(case_id,memory=memory,cancel_jobs=lambda *a:None,session_factory=factory)
    await privacy.delete_account(case_id,memory=memory,cancel_jobs=lambda *a:pytest.fail('completed erasure retried external work'),session_factory=factory)
    assert not cache.data
    with factory() as db:
        assert db.get(m.User,user_id) is None and db.get(m.User,other_id)
        for table in m.Base.metadata.tables.values():
            if 'user_id' in table.c:
                assert db.execute(select(table.c.user_id).where(table.c.user_id==user_id).limit(1)).first() is None,table.name
        assert db.query(m.AggregateRecord).filter_by(record_kind='sealed_cell').count()==1
        receipt=db.get(m.PrivacyRequest,case_id)
        assert db.get(m.AccessIdentity,identity_id) is None
        assert db.query(m.DeploymentRosterEntry).filter_by(access_identity_id=identity_id).count()==0
        assert receipt.source_operation_id is None and receipt.user_id is None and receipt.evidence=={'result':'account_erased'}
        assert db.query(m.PlanCompletion).join(m.AIPlan,m.PlanCompletion.plan_id==m.AIPlan.id).filter(m.AIPlan.user_id==user_id).count()==0


@pytest.mark.parametrize('table',['user_facts','user_daily_logs'])
def test_sensitive_store_nonempty_guard_is_transactional_and_unknown_age_expires(table):
    with fresh_database('20261015_access_authority') as (engine,url):
        with engine.begin() as db:
            user_id=db.execute(text('INSERT INTO users (tg_id) VALUES (123) RETURNING id')).scalar_one()
            if table=='user_facts':db.execute(text("INSERT INTO user_facts (user_id,category,content) VALUES (:id,'medical','disposable only')"),{'id':user_id})
            else:db.execute(text("INSERT INTO user_daily_logs (user_id,log_date,note) VALUES (:id,'2026-01-01','disposable only')"),{'id':user_id})
        result=_run_alembic(url,'head',check=False)
        assert result.returncode!=0 and 'sensitive stores nonempty' in result.stderr
        with engine.begin() as db:
            assert db.execute(text('SELECT version_num FROM alembic_version')).scalar_one()=='20261015_access_authority'
            db.execute(text('DELETE FROM '+table))
            db.execute(text("INSERT INTO chat_history (user_id,role,text,created_at) VALUES (:id,'user','unknown-age',NULL)"),{'id':user_id})
        _run_alembic(url,'head')
        with engine.connect() as db:
            assert db.execute(text("SELECT expires_at<=now() FROM chat_history WHERE text='unknown-age'")).scalar_one()
            assert db.execute(text("SELECT to_regclass('user_facts') IS NULL AND to_regclass('user_daily_logs') IS NULL")).scalar_one()


async def test_cache_writer_cannot_repopulate_after_deletion(engine):
    from threading import Event
    factory=sessionmaker(engine,expire_on_commit=False)
    with factory() as db:
        user,_,_=subject(db)
        db.add(m.ChatHistory(user_id=user.id,role='user',text='racing-private-text'))
        request=privacy.receive_request(db,tg_id=user.tg_id,kind='deletion',source_operation_id='racing-delete')
        user_id,case_id=user.id,request.id;db.commit()
    started=Event();release=Event()
    class SlowCache(Cache):
        def set(self,*args,**kwargs):
            started.set()
            assert release.wait(5)
            return super().set(*args,**kwargs)
    cache=SlowCache()
    memory=SessionMemory(redis_client=AsyncCache(cache),cache_client=cache,session_factory=factory)
    reader=asyncio.create_task(memory.get_recent_messages(user_id))
    assert await asyncio.to_thread(started.wait,5)
    erasure=asyncio.create_task(privacy.delete_account(case_id,memory=memory,cancel_jobs=lambda *a:None,session_factory=factory))
    # Keep the event loop free while the deletion worker waits for the DB lock.
    await asyncio.sleep(.05)
    release.set()
    await reader;await erasure
    assert not cache.data
    await memory.set_pending_action(user_id,'collect_evening_time_for_medium')
    assert not cache.data


async def test_real_redis_snapshot_has_ttl_and_erasure_removes_legacy_keys(engine):
    redis_url=os.environ.get('WP04_2_TEST_REDIS_URL')
    if not redis_url:pytest.skip('explicit disposable Redis URL required')
    from redis import Redis
    from redis.asyncio import Redis as AsyncRedis
    cache=Redis.from_url(redis_url,decode_responses=True,socket_timeout=2)
    async_cache=AsyncRedis.from_url(redis_url,decode_responses=True,socket_timeout=2)
    factory=sessionmaker(engine,expire_on_commit=False)
    with factory() as db:
        user,_,_=subject(db);user_id=user.id
        db.add(m.ChatHistory(user_id=user_id,role='user',text='retained-real-redis'))
        db.commit()
    memory=SessionMemory(redis_client=async_cache,cache_client=cache,session_factory=factory)
    key=memory._messages_key(user_id)
    try:
        cache.set(key,'forged-cache',ex=10)
        result=await memory.get_recent_messages(user_id)
        assert [r['text'] for r in result]==['retained-real-redis']
        assert 0<cache.ttl(key)<=86400
        assert json.loads(cache.get(key))==result
        cache.delete(key)
        assert await memory.get_recent_messages(user_id)==result
        cache.set(f'session:{user_id}:messages','legacy-secret')
        await memory.set_pending_action(user_id,'collect_evening_time_for_medium')
        await memory.delete_user(user_id)
        assert not cache.exists(key,memory._pending_action_key(user_id),f'session:{user_id}:messages')
    finally:
        await async_cache.aclose();cache.delete(key,memory._pending_action_key(user_id),f'session:{user_id}:messages');cache.close()


def test_report_http_surface_checks_real_grant_and_returns_neutral_private_failures(engine,monkeypatch):
    factory=sessionmaker(engine,expire_on_commit=False)
    with factory() as db:
        user,plan=source_plan(db)
        completion=tokens.issue_report_grant(db,user.id,plan.id,'completion_report')
        pulse=tokens.issue_report_grant(db,user.id,plan.id,'pulse_report')
        grant_id=db.query(m.ReportAccessGrant.id).filter_by(user_id=user.id,purpose='completion_report').scalar()
        db.commit()
    monkeypatch.setattr(api,'SessionLocal',factory)
    monkeypatch.setattr(api,'templates',None)
    monkeypatch.setattr(api,'build_completion_metrics',lambda *a:SimpleNamespace(completion_rate=.5))
    monkeypatch.setattr(api,'build_completion_report',lambda *a:'personal report')
    monkeypatch.setattr(api,'_pick_observation',lambda *a:'observation')
    monkeypatch.setattr(api,'get_plan_timeline',lambda *a:[])
    client=TestClient(api.app)
    valid=client.get('/report/'+completion)
    assert valid.status_code==200 and valid.headers['cache-control']=='no-store' and valid.headers['referrer-policy']=='no-referrer'
    denied=[client.get('/report/'+pulse),client.get('/pulse/'+completion),client.get('/report/'+'x'*43)]
    with factory() as db:
        tokens.revoke_report_grant(db,grant_id);db.commit()
    denied.append(client.get('/report/'+completion))
    assert {r.status_code for r in denied}=={404}
    assert len({r.text for r in denied})==1


async def test_erasing_old_account_keeps_other_accounts_shared_access_identity(engine):
    factory=sessionmaker(engine,expire_on_commit=False)
    with factory() as db:
        old,dep,_=subject(db)
        old_enrollment=db.query(m.DeploymentEnrollment).filter_by(user_id=old.id,ended_at=None).one()
        old_enrollment.ended_at=privacy.utcnow()
        ent_id=old_enrollment.entitlement_id
        db.flush()
        current=m.User(tg_id=uuid4().int % 10**12,is_active=True)
        db.add(current);db.flush()
        db.add(m.DeploymentEnrollment(user_id=current.id,deployment_id=dep.id,entitlement_id=ent_id,
            enrolled_at=privacy.utcnow(),attribution_source='testnet'))
        case=privacy.receive_request(db,tg_id=old.tg_id,kind='deletion',source_operation_id='old-device:delete')
        old_id,current_id,case_id=old.id,current.id,case.id
        db.commit()
    cache=Cache();memory=SessionMemory(redis_client=AsyncCache(cache),cache_client=cache,session_factory=factory)
    await privacy.delete_account(case_id,memory=memory,cancel_jobs=lambda *a:None,session_factory=factory)
    with factory() as db:
        assert db.get(m.User,old_id) is None
        assert access.require_access(db,current_id).entitlement_id==ent_id
        assert db.query(m.DeploymentEnrollment).filter_by(user_id=old_id).count()==0


async def test_erasure_removes_actual_persisted_scheduler_jobs(engine,monkeypatch):
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
    from app import scheduler as scheduling
    factory=sessionmaker(engine,expire_on_commit=False)
    with factory() as db:
        user,plan=source_plan(db)
        step_id=db.query(m.AIPlanStep.id).join(m.AIPlanDay).filter(m.AIPlanDay.plan_id==plan.id).first()[0]
        request=privacy.receive_request(db,tg_id=user.tg_id,kind='deletion',source_operation_id='persistent-jobs:delete')
        user_id,chat_id,plan_id,case_id=user.id,user.tg_id,plan.id,request.id;db.commit()
    jobs=BackgroundScheduler(jobstores={'default':SQLAlchemyJobStore(engine=engine)},timezone='UTC')
    jobs.start(paused=True)
    monkeypatch.setattr(scheduling,'scheduler',jobs)
    monkeypatch.setattr(scheduling,'SessionLocal',factory)
    try:
        jobs.add_job('app.scheduler:send_scheduled_message','date',run_date=privacy.utcnow()+timedelta(days=1),
            id='historical-custom-id',args=[chat_id,'',step_id])
        jobs.add_job('app.scheduler:check_plan_completions','date',run_date=privacy.utcnow()+timedelta(days=1),id=f'completion_{plan_id}')
        jobs.add_job('app.scheduler:check_plan_completions','date',run_date=privacy.utcnow()+timedelta(days=1),id='unrelated-global-job')
        cache=Cache();memory=SessionMemory(redis_client=AsyncCache(cache),cache_client=cache,session_factory=factory)
        await privacy.delete_account(case_id,memory=memory,cancel_jobs=scheduling.cancel_privacy_jobs,session_factory=factory)
        assert [job.id for job in jobs.get_jobs()]==['unrelated-global-job']
    finally:
        jobs.shutdown()


def test_operator_can_page_past_one_hundred_stuck_cases(db):
    at=privacy.utcnow()-timedelta(days=31)
    cases=[privacy.record_manual_request(db,kind='export',received_at=at) for _ in range(101)]
    first,cursor=privacy.pending_request_page(db)
    assert len(first)==100 and cursor is not None
    # Completing the anchor does not make the remaining page inaccessible.
    anchor=db.get(m.PrivacyRequest,__import__('uuid').UUID(cursor))
    anchor.state='completed';db.flush()
    second,last=privacy.pending_request_page(db,after=cursor)
    assert len(second)==1 and last is None
    assert {r.id for r in first+second}=={r.id for r in cases}
    assert all(r.due_at<privacy.utcnow() for r in second)


async def test_deletion_retry_rotates_past_ten_broken_cases(engine,monkeypatch):
    from app import scheduler as scheduling
    from app import session_memory
    factory=sessionmaker(engine,expire_on_commit=False)
    ids=[];case_ids=[]
    with factory() as db:
        for i in range(11):
            user,_,_=subject(db)
            request=privacy.receive_request(db,tg_id=user.tg_id,kind='deletion',source_operation_id=f'backlog:{i}')
            privacy.begin_account_deletion(db,request.id)
            ids.append(user.id);case_ids.append(request.id)
        db.commit()
    attempts=[]
    class Memory:
        async def delete_user(self,user_id):
            attempts.append(user_id)
            if user_id in ids[:10]:raise ConnectionError('subject-specific external failure')
    monkeypatch.setattr(scheduling,'SessionLocal',factory)
    monkeypatch.setattr(m,'SessionLocal',factory)
    monkeypatch.setattr(session_memory,'SessionMemory',Memory)
    monkeypatch.setattr(scheduling,'cancel_privacy_jobs',lambda *a:None)
    await scheduling.privacy_maintenance()
    assert set(attempts)==set(ids[:10])
    with factory() as db:
        assert db.get(m.PrivacyRequest,case_ids[10]).last_attempted_at is None
    attempts.clear()
    await scheduling.privacy_maintenance()
    assert ids[10] in attempts
    with factory() as db:
        assert db.get(m.PrivacyRequest,case_ids[10]).state=='completed'
        assert db.get(m.User,ids[10]) is None
        for case_id in case_ids[:10]:
            request=db.get(m.PrivacyRequest,case_id)
            assert request.state=='deleting' and request.last_attempted_at is not None


async def test_pulse_revocation_during_scan_skips_only_that_owner(engine,monkeypatch):
    from app import scheduler as scheduling
    from unittest.mock import AsyncMock
    factory=sessionmaker(engine,expire_on_commit=False)
    with factory() as db:
        first,first_plan=source_plan(db)
        second,second_plan=source_plan(db)
        first_id,second_id,second_chat=first.id,second.id,second.tg_id
        selected_plans={first_plan.id,second_plan.id}
        ent_id=db.query(m.DeploymentEnrollment.entitlement_id).filter_by(user_id=first.id,ended_at=None).scalar()
        db.commit()
    original=scheduling.issue_report_grant
    revoked=False
    def revoke_before_issue(db,user_id,plan_id,purpose):
        nonlocal revoked
        if user_id==first_id and not revoked:
            with factory() as other:
                access.revoke_entitlement(other,entitlement_id=ent_id);other.commit()
            revoked=True
        return original(db,user_id,plan_id,purpose)
    monkeypatch.setattr(scheduling,'issue_report_grant',revoke_before_issue)
    monkeypatch.setattr(scheduling,'PULSE_THRESHOLDS',{'SHORT':[1]})
    monkeypatch.setattr(scheduling,'derive_current_day',lambda db,plan_id,*a:1 if plan_id in selected_plans else 0)
    monkeypatch.setattr(scheduling,'outbound_claim',lambda *a,**k:privacy.utcnow())
    bot=SimpleNamespace(send_message=AsyncMock())
    with factory() as db:
        await scheduling.check_pulse_triggers(db,bot)
    assert revoked
    bot.send_message.assert_awaited_once()
    assert bot.send_message.call_args.args[0]==second_chat
    with factory() as db:
        assert db.query(m.ReportAccessGrant).filter_by(user_id=first_id,purpose='pulse_report').count()==0
        assert db.query(m.UserEvent).filter_by(user_id=first_id,event_name='pulse_sent').count()==0
        assert db.query(m.ReportAccessGrant).filter_by(user_id=second_id,purpose='pulse_report').count()==1
        assert db.query(m.UserEvent).filter_by(user_id=second_id,event_name='pulse_sent').count()==1


@pytest.mark.parametrize('notice_state',['unacknowledged','repinned','unavailable','retired'])
async def test_freeform_notice_reminder_releases_ingress_locks(engine,monkeypatch,notice_state):
    """Real separate PG sessions expose the former nested-lock self-block."""
    from unittest.mock import AsyncMock
    from app import telegram
    factory=sessionmaker(engine,expire_on_commit=False)
    with factory() as db:
        user,dep,notice=subject(db)
        if notice_state=='repinned':
            privacy.acknowledge_notice(db,user.id,notice.id,f'{user.id}:old-notice')
            notice=privacy.register_notice(db,version=uuid4().hex,notice_text='New exact notice',location='fixture-v2')
            dep.notice_version_id=notice.id
        elif notice_state=='unavailable':
            notice=m.PrivacyNoticeVersion(version=uuid4().hex,published_at=privacy.utcnow(),
                content_digest=uuid4().hex,content_location='unpublished fixture',is_current=False)
            db.add(notice);db.flush();dep.notice_version_id=notice.id
        elif notice_state=='retired':
            notice.retired_at=privacy.utcnow()
        user_id,tg_id,notice_id=user.id,user.tg_id,notice.id
        db.commit()
    @contextmanager
    def ingress_session():
        with factory() as db:
            # The nested reminder used to time out here waiting for its caller.
            db.execute(text("SET LOCAL lock_timeout = '300ms'"))
            yield db
    monkeypatch.setattr(telegram,'SessionLocal',ingress_session)
    coach=AsyncMock(return_value={'reply_text':'accepted reply'})
    send=AsyncMock()
    monkeypatch.setattr(telegram,'handle_incoming_message',coach)
    monkeypatch.setattr(telegram,'_send_agent_response',send)
    message=SimpleNamespace(text='must not be retained before acknowledgement',message_id=1,
        from_user=SimpleNamespace(id=tg_id,username=None,first_name=None),
        chat=SimpleNamespace(id=tg_id,type='private'),answer=AsyncMock())
    await telegram.on_text(message)
    coach.assert_not_awaited();send.assert_not_awaited()
    assert message.answer.await_count>=1
    with factory() as db:
        assert db.query(m.ChatHistory).filter_by(user_id=user_id).count()==0
        assert db.query(m.UserEvent).filter_by(user_id=user_id,event_name='user_message').count()==0
    if notice_state in {'unacknowledged','repinned'}:
        with factory() as db:
            privacy.acknowledge_notice(db,user_id,notice_id,f'{user_id}:accepted-current-notice');db.commit()
        reminders=message.answer.await_count
        for message_id in (2,3):
            message.message_id=message_id;message.text=f'accepted turn {message_id}'
            await telegram.on_text(message)
        # Stored acknowledgement is reused: no repeated confirmation dialog.
        assert message.answer.await_count==reminders
        assert coach.await_count==2 and send.await_count==2
        with factory() as db:
            assert db.query(m.ChatHistory).filter_by(user_id=user_id).count()==2
            assert db.query(m.UserEvent).filter_by(user_id=user_id,event_name='user_message').count()==2


def test_notice_cache_reads_receipt_once_and_rechecks_version_and_access(engine):
    from sqlalchemy import event
    from app import privacy_notice_cache as cache
    factory=sessionmaker(engine,expire_on_commit=False)
    with factory() as db:
        user,dep,notice=subject(db)
        uid,did,nid=user.id,dep.id,notice.id
        privacy.acknowledge_notice(db,uid,nid,'cache:seed');db.commit()
        cache.forget_user(db,uid)  # A restarted/evicted worker starts cold.
    reads=[]
    def observe(conn,cursor,statement,parameters,context,executemany):
        normalized=statement.lower().lstrip()
        if normalized.startswith('select') and 'from notice_acknowledgements' in normalized:
            reads.append(statement)
    event.listen(engine,'before_cursor_execute',observe)
    try:
        for _ in range(5):
            with factory() as db:
                privacy.pinned_notice(db,uid,acknowledged=True)
                cache.commit_read_check(db)
        assert len(reads)==1
        with factory() as db:
            newer=privacy.register_notice(db,version=uuid4().hex,notice_text='Changed cache notice',location='fixture')
            db.get(m.Deployment,did).notice_version_id=newer.id
            new_id=newer.id;db.commit()
        with factory() as db:
            with pytest.raises(privacy.NoticeRequired):privacy.pinned_notice(db,uid,acknowledged=True)
        assert len(reads)==2
        with factory() as db:
            privacy.acknowledge_notice(db,uid,new_id,'cache:new-version');db.commit()
        with factory() as db:
            privacy.pinned_notice(db,uid,acknowledged=True)
            assert cache.has_committed_ack(db,uid,did,new_id)
            assert not cache.has_committed_ack(db,uid,did,nid)
            db.get(m.PrivacyNoticeVersion,new_id).retired_at=privacy.utcnow();db.commit()
        with factory() as db:
            with pytest.raises(privacy.NoticeRequired):privacy.pinned_notice(db,uid,acknowledged=True)
        with factory() as db:
            db.get(m.User,uid).is_active=False;db.commit()
        with factory() as db:
            with pytest.raises(access.AccessDenied):privacy.pinned_notice(db,uid,acknowledged=True)
        assert len(reads)==2  # New acknowledgement commit warms immediately.
    finally:
        event.remove(engine,'before_cursor_execute',observe)


def test_notice_cache_only_root_commit_promotes_and_invalidation_wins(engine):
    from app import privacy_notice_cache as cache
    factory=sessionmaker(engine,expire_on_commit=False)
    with factory() as db:
        user,dep,notice=subject(db)
        uid,did,nid=user.id,dep.id,notice.id;db.commit()
    with factory() as db:
        privacy.acknowledge_notice(db,uid,nid,'cache:rolled-back')
        assert not cache.has_committed_ack(db,uid,did,nid)
        db.rollback()
        assert not cache.has_committed_ack(db,uid,did,nid)
        with db.begin_nested():
            privacy.acknowledge_notice(db,uid,nid,'cache:committed')
        assert not cache.has_committed_ack(db,uid,did,nid)
        db.commit()
        assert cache.has_committed_ack(db,uid,did,nid)
        cache.forget_user(db,uid)
        privacy.pinned_notice(db,uid,acknowledged=True)
        cache.forget_user(db,uid)
        db.commit()
        assert not cache.has_committed_ack(db,uid,did,nid)
        privacy.pinned_notice(db,uid,acknowledged=True)
        db.close()  # Reusing a Session after close must not promote stale hints.
        db.commit()
        assert not cache.has_committed_ack(db,uid,did,nid)
        with pytest.raises(privacy.NoticeRequired):
            other,_,_=subject(db)
            privacy.pinned_notice(db,other.id,acknowledged=True)

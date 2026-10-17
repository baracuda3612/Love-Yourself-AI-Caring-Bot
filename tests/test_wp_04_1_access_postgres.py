"""Real migrated disposable PostgreSQL, caller proof and access races."""
import json
import os
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor

import psycopg2
from psycopg2 import sql
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app import access, access_proof, api, access_http
from app.db import (AccessEntitlement, AccessIdentity, AccessProof, Deployment,
    DeploymentEnrollment, DeploymentInvitation, DeploymentRosterVersion, User)
from scripts.test_migrations import _run_alembic
from tests.access_fixtures import deployment, enroll

pytestmark = pytest.mark.skipif(os.environ.get('WP04_1_POSTGRES_REHEARSAL') != '1', reason='fresh disposable access rehearsal only')


@pytest.fixture(scope='module')
def engine():
    admin_url = os.environ['WP04_1_TEST_ADMIN_URL']
    name = 'ly_wp041_' + uuid4().hex
    admin = psycopg2.connect(admin_url); admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    engine = None
    try:
        _run_alembic(admin_url.rsplit('/',1)[0] + '/' + name, 'head')
        engine = create_engine(admin_url.rsplit('/',1)[0] + '/' + name)
        yield engine
    finally:
        if engine: engine.dispose()
        with admin.cursor() as cur:
            cur.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s AND pid<>pg_backend_pid()', (name,))
            cur.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
        admin.close()


@pytest.fixture
def db(engine, monkeypatch):
    monkeypatch.setattr(access_proof, "rate_limit", lambda *a, **k: None)
    with engine.connect() as connection:
        tx = connection.begin()
        with Session(bind=connection) as session:
            yield session
        tx.rollback()


def actor():
    return SimpleNamespace(id=uuid4().int % 10**12 + 1, username=None, first_name=None)


def roster(db, dep, emails, *, mode='full_snapshot', rows=None, total=None):
    preview = access.preview_roster(db, deployment_id=dep.id, mode=mode,
        rows=rows or [{'email': email, 'action':'add'} for email in emails],
        expected_total=len(emails) if total is None else total, source_ref='operator-approved-fixture', operator='founder')
    access.apply_roster(db, deployment_id=dep.id, version_id=preview.version_id,
        confirmation=preview.confirmation, counts=preview.counts, operator='founder')
    return preview


def verified(db, dep, email='member@client.org'):
    roster(db, dep, [email])
    raw = access_proof.begin_email(db, deployment_id=dep.id, email=email, browser='b'*43)
    return access_proof.finish_email(db, deployment_id=dep.id, raw=raw, browser='b'*43)


def finish_oidc(db, *, deployment_id, provider, state, code, browser):
    prepared=access_proof.prepare_oidc(db,deployment_id=deployment_id,provider=provider,state=state,browser=browser)
    db.commit()  # HTTP consumes once before the network exchange.
    claims=access_proof.verify_oidc(prepared,state=state,code=code)
    return access_proof.finish_oidc(db,prepared=prepared,claims=claims,browser=browser)


def test_fresh_database_and_redis_binding_fail_closed(db,monkeypatch):
    from app import access_environment
    from tests.test_wp_04_1_access_security import inventory
    inventory(monkeypatch)
    class Redis:
        value=None
        def set(self,key,value,nx):
            if self.value is None:self.value=value
        def get(self,key):return self.value
    cache=Redis()
    monkeypatch.setattr(access_environment.redis.Redis,'from_url',lambda *a,**k:cache)
    with pytest.raises(access.AccessDenied):access_environment.audit_environment(db)
    access_environment.bind_environment(db)
    access_environment.audit_environment(db)
    cache.value='production:other-runtime'
    with pytest.raises(access.AccessDenied):access_environment.audit_environment(db)
    with pytest.raises(access.AccessDenied):access_environment.bind_environment(db)


def test_complete_production_proof_token_enrollment_return_revocation(db, monkeypatch):
    monkeypatch.setattr(access.settings, 'ENVIRONMENT', 'prod')
    dep = deployment(db, environment='production', zone='America/New_York')
    raw = verified(db, dep)
    tg = actor()
    user, created = access.redeem_handoff(db, raw, tg)
    assert created and user.timezone == 'America/New_York'
    membership = access.require_access(db, user.id)
    assert db.query(AccessIdentity).one().normalized_email == 'member@client.org'
    invitation = db.query(DeploymentInvitation).one()
    assert raw not in invitation.token_digest and invitation.redeemed_at
    with pytest.raises(access.AccessDenied): access.redeem_handoff(db, raw, tg)
    # Returning bare entry needs no proof/token, and does not create a user.
    from app.telegram import _ensure_user
    # _ensure_user commits; guard itself is tested here within outer transaction.
    assert access.require_access(db, user.id).id == membership.id
    entitlement = db.get(AccessEntitlement, membership.entitlement_id)
    access.revoke_entitlement(db, entitlement_id=entitlement.id)
    assert not access.may_access(db, user.id)
    assert db.get(User, user.id) and db.get(DeploymentEnrollment, membership.id)


def test_missed_rejected_delta_and_confirmed_snapshot(db, monkeypatch):
    monkeypatch.setattr(access.settings, 'ENVIRONMENT', 'prod')
    dep = deployment(db, environment='production')
    roster(db, dep, ['a@client.org','b@client.org'])
    ent = access.grant_verified(db, dep.id, access.email_digest('a@client.org'))
    previous = ent.confirming_roster_version_id
    with pytest.raises(access.AccessDenied):
        access.preview_roster(db, deployment_id=dep.id, mode='unknown', rows=[], expected_total=0, source_ref='bad', operator='founder')
    assert ent.revoked_at is None and ent.confirming_roster_version_id == previous
    preview = access.preview_roster(db, deployment_id=dep.id, mode='full_snapshot', rows=[{'email':'b@client.org','action':'add'}], expected_total=1, source_ref='expected-small-confirmed', operator='founder')
    assert preview.counts == {'add':0,'keep':1,'revoke':1,'total':1}
    assert ent.revoked_at is None  # preview is not apply
    roster(db, dep, ['a@client.org','b@client.org','c@client.org'], mode='delta', rows=[{'email':'c@client.org','action':'add'}])
    assert ent.revoked_at is None
    with pytest.raises(access.AccessDenied):
        access.apply_roster(db, deployment_id=dep.id, version_id=preview.version_id, confirmation=preview.confirmation, counts=preview.counts, operator='founder')
    roster(db, dep, ['b@client.org','c@client.org'])
    assert ent.revoked_at is not None


@pytest.mark.parametrize('case', ['empty','missing-mode','duplicate','count','domain','action'])
def test_roster_invalid_input_does_not_revoke(db, monkeypatch, case):
    monkeypatch.setattr(access.settings,'ENVIRONMENT','prod')
    dep = deployment(db, environment='production')
    roster(db, dep, ['a@client.org'])
    ent = access.grant_verified(db, dep.id, access.email_digest('a@client.org'))
    dep.access_policy = {'allowed_domains':['client.org']}; db.flush()
    rows = [{'email':'a@client.org','action':'add'}]; mode='full_snapshot'; total=1
    if case == 'empty': rows=[]
    if case == 'missing-mode': mode=None
    if case == 'duplicate': rows=rows*2
    if case == 'count': total=2
    if case == 'domain': rows=[{'email':'a@other.org','action':'add'}]
    if case == 'action': rows=[{'email':'a@client.org','action':'revoke'}]
    with pytest.raises(access.AccessDenied):
        access.preview_roster(db, deployment_id=dep.id, mode=mode, rows=rows, expected_total=total, source_ref='bad', operator='founder')
    assert ent.revoked_at is None


@pytest.mark.parametrize('case', ['expired','revoked','spent','environment','version','wrong-account','wrong-deployment','commercial-end'])
def test_invalid_tokens_never_create_account(db, monkeypatch, case):
    dep = deployment(db)
    ent = access.founder_grant(db, deployment_id=dep.id, operator='founder', label=uuid4().hex)
    raw = access.issue_handoff(db, ent.id); invitation=db.query(DeploymentInvitation).filter_by(entitlement_id=ent.id).one()
    tg=actor()
    if case == 'expired': invitation.expires_at=access.now_utc()-timedelta(seconds=1); invitation.issued_at=invitation.expires_at-timedelta(minutes=10)
    if case == 'revoked': access.revoke_entitlement(db, entitlement_id=ent.id)
    if case == 'spent': access.redeem_handoff(db,raw,actor())
    if case == 'environment': monkeypatch.setattr(access.settings,'ENVIRONMENT','prod')
    if case == 'version': monkeypatch.setattr(access.settings,'ACCESS_KEY_VERSION',2)
    if case == 'wrong-account': access.redeem_handoff(db,raw,actor()); raw=access.issue_handoff(db,ent.id)
    if case == 'wrong-deployment':
        user=User(tg_id=tg.id);db.add(user);db.flush();enroll(db,user)
    if case == 'commercial-end': dep.ends_at=access.now_utc()-timedelta(seconds=1)
    db.flush(); before=db.query(User).count()
    with pytest.raises(access.AccessDenied, match='^access_unavailable$'): access.redeem_handoff(db,raw,tg)
    assert db.query(User).count()==before


def test_pauses_renewal_timezone_and_history(db):
    dep=deployment(db, zone='Europe/London')
    ent=access.founder_grant(db,deployment_id=dep.id,operator='founder',label='renew')
    user,_=access.redeem_handoff(db,access.issue_handoff(db,ent.id),actor())
    assert user.timezone=='Europe/London'
    user.timezone='Asia/Tokyo'; assert user.timezone=='Asia/Tokyo'
    dep.enrollment_open=False; db.flush(); access.require_access(db,user.id)
    dep.delivery_enabled=False; db.flush()
    assert access.may_access(db,user.id) and not access.may_access(db,user.id,delivery=True)
    dep.ends_at=access.now_utc()-timedelta(seconds=1);db.flush();assert not access.may_access(db,user.id)
    dep.ends_at=access.now_utc()+timedelta(days=365);db.flush();assert access.may_access(db,user.id)
    assert db.query(DeploymentEnrollment).filter_by(user_id=user.id).count()==1
    user.timezone=None;dep.timezone_mode='distributed';db.flush()
    with pytest.raises(access.AccessDenied): access.require_planning_timezone(user)


def test_email_unknown_expired_replayed_cross_browser_and_deployment(db,monkeypatch):
    monkeypatch.setattr(access.settings,'ENVIRONMENT','prod')
    dep=deployment(db,environment='production');roster(db,dep,['a@client.org'])
    raw=access_proof.begin_email(db,deployment_id=dep.id,email='unknown@client.org',browser='b'*43)
    with pytest.raises(access.AccessDenied):access_proof.finish_email(db,deployment_id=dep.id,raw=raw,browser='b'*43)
    raw=access_proof.begin_email(db,deployment_id=dep.id,email='a@client.org',browser='b'*43)
    with pytest.raises(access.AccessDenied):access_proof.finish_email(db,deployment_id=dep.id,raw=raw,browser='c'*43)
    other=deployment(db)
    with pytest.raises(access.AccessDenied):access_proof.finish_email(db,deployment_id=other.id,raw=raw,browser='b'*43)
    access_proof.finish_email(db,deployment_id=dep.id,raw=raw,browser='b'*43)
    with pytest.raises(access.AccessDenied):access_proof.finish_email(db,deployment_id=dep.id,raw=raw,browser='b'*43)
    proof=db.query(AccessProof).filter_by(secret_digest=access.digest(raw,'proof')).one()
    assert raw not in proof.secret_digest


def test_atomic_roster_rollback(db,monkeypatch):
    monkeypatch.setattr(access.settings,'ENVIRONMENT','prod')
    dep=deployment(db,environment='production'); roster(db,dep,['a@client.org','b@client.org'])
    ent=access.grant_verified(db,dep.id,access.email_digest('a@client.org'))
    preview=access.preview_roster(db,deployment_id=dep.id,mode='full_snapshot',rows=[{'email':'b@client.org','action':'add'}],expected_total=1,source_ref='rollback',operator='founder')
    savepoint=db.begin_nested()
    access.apply_roster(db,deployment_id=dep.id,version_id=preview.version_id,confirmation=preview.confirmation,counts=preview.counts,operator='founder')
    assert ent.revoked_at
    savepoint.rollback(); db.refresh(ent)
    assert ent.revoked_at is None
    assert db.query(DeploymentRosterVersion).filter_by(deployment_id=dep.id,is_current=True).one().version==1


def test_concurrent_redemption_single_winner(engine):
    with Session(engine) as db:
        dep=deployment(db);ent=access.founder_grant(db,deployment_id=dep.id,operator='founder',label='concurrent')
        raw=access.issue_handoff(db,ent.id); entitlement_id=ent.id;db.commit()
    def redeem(_):
        with Session(engine) as db:
            try:
                user,_=access.redeem_handoff(db,raw,actor());db.commit();return user.id
            except access.AccessDenied:
                db.rollback();return None
    with ThreadPoolExecutor(max_workers=4) as pool: results=list(pool.map(redeem,range(4)))
    assert sum(result is not None for result in results)==1
    with Session(engine) as db:
        assert db.query(DeploymentEnrollment).filter_by(entitlement_id=entitlement_id).count()==1


@pytest.mark.parametrize('target', ['same-user','same-deployment','cross-deployment','nonexistent','revoked'])
def test_http_owner_and_neutral_cross_user_denial(db, monkeypatch, target):
    from tests.test_wp_04_1_access_security import webapp
    dep=deployment(db);user=User(tg_id=actor().id,timezone='UTC');db.add(user);db.flush();membership=enroll(db,user,target=dep)
    other=User(tg_id=actor().id,timezone='UTC');db.add(other);db.flush();enroll(db,other,target=dep if target=='same-deployment' else None)
    if target=='revoked':access.revoke_entitlement(db,entitlement_id=membership.entitlement_id)
    @contextmanager
    def session():yield db
    monkeypatch.setattr(api,'SessionLocal',session)
    monkeypatch.setattr(access_proof,'rate_limit',lambda *a,**k:None)
    changes=[]
    def change(_db,**kwargs):
        changes.append(kwargs);raise api.LifecycleTransitionError('test_mutation_boundary')
    monkeypatch.setattr(api,'change_delivery_time',change)
    target_id=user.id if target in {'same-user','revoked'} else other.id if target!='nonexistent' else 99999999
    response=TestClient(api.app).post('/user/time-slots',params={'user_id':target_id},headers={'X-Telegram-Init-Data':webapp(user.tg_id),'Idempotency-Key':'stable'},json={'DAY':'14:00'})
    assert response.status_code==(400 if target=='same-user' else 403)
    assert len(changes)==(1 if target=='same-user' else 0)
    if target!='same-user':assert response.json()=={'detail':'access_unavailable'}
    assert response.headers['cache-control']=='no-store'


def test_owner_http_idempotency_preserves_deferred_lifecycle(db,monkeypatch):
    from app.content_library import load_content_library
    from app.db import PlanLifecycleOperation
    from app.lifecycle import transition_current_plan
    from tests.test_wp_03_5_postgres_completion import source_plan
    from tests.test_wp_04_1_access_security import webapp
    load_content_library(db)
    user,plan=source_plan(db)
    transition_current_plan(db,user_id=user.id,operation='pause',source_operation_id='fixture:pause')
    @contextmanager
    def session():yield db
    monkeypatch.setattr(api,'SessionLocal',session)
    monkeypatch.setattr(api,'reconcile_scheduler_effects',lambda result:result)
    monkeypatch.setattr(access_proof,'rate_limit',lambda *a,**k:None)
    client=TestClient(api.app)
    headers={'X-Telegram-Init-Data':webapp(user.tg_id),'Idempotency-Key':'owner-stable'}
    first=client.post('/user/time-slots',params={'user_id':user.id},json={'DAY':'15:30'},headers=headers)
    second=client.post('/user/time-slots',params={'user_id':user.id},json={'DAY':'15:30'},headers=headers)
    assert first.status_code==second.status_code==200,first.text
    assert first.json()['schedule_state']==second.json()['schedule_state']
    assert second.json()['updated_steps']==0
    assert first.json()['schedule_state']=='deferred'
    assert db.query(PlanLifecycleOperation).filter_by(user_id=user.id,source_operation_id='owner-stable:day').count()==1


def test_operator_replacement_requires_fresh_proof_and_keeps_old_account(db):
    from app.db import AccessAdminOperation
    dep=deployment(db);ent=access.founder_grant(db,deployment_id=dep.id,operator='founder',label='replacement')
    old,_=access.redeem_handoff(db,access.issue_handoff(db,ent.id),actor())
    raw=access.issue_handoff(db,ent.id)
    new_actor=actor()
    with pytest.raises(access.AccessDenied):access.redeem_handoff(db,raw,new_actor)
    access.end_enrollment_for_replacement(db,user_id=old.id,handoff=raw,operator='founder')
    new,_=access.redeem_handoff(db,raw,new_actor)
    assert new.id!=old.id and db.get(User,old.id)
    assert not access.may_access(db,old.id) and access.may_access(db,new.id)
    assert db.query(AccessAdminOperation).filter_by(action='end_enrollment',deployment_id=dep.id).count()==1


@pytest.mark.parametrize('provider',['google','microsoft'])
def test_oidc_code_state_nonce_and_verified_roster_chain(db,monkeypatch,provider):
    from urllib.parse import urlsplit,parse_qs
    monkeypatch.setattr(access.settings,'GOOGLE_CLIENT_ID','google-client')
    monkeypatch.setattr(access.settings,'GOOGLE_CLIENT_SECRET','private-google-secret')
    monkeypatch.setattr(access.settings,'MICROSOFT_CLIENT_ID','ms-client')
    monkeypatch.setattr(access.settings,'MICROSOFT_CLIENT_SECRET','private-ms-secret')
    tenant='12345678-1234-1234-1234-123456789abc'
    monkeypatch.setattr(access.settings,'ENVIRONMENT','prod')
    dep=deployment(db,environment='production');dep.access_policy={'allowed_domains':['client.org'],'google_domain':'client.org','microsoft_tenant':tenant};db.flush()
    roster(db,dep,['member@client.org'])
    browser='b'*43
    url=access_proof.begin_oidc(db,deployment_id=dep.id,provider=provider,browser=browser)
    query=parse_qs(urlsplit(url).query)
    assert query['scope']==['openid email'] and query['code_challenge_method']==['S256']
    assert 'private-' not in url
    state=query['state'][0];nonce=query['nonce'][0]
    monkeypatch.setattr(access_proof,'exchange_code',lambda *a:'signed-token')
    config=access_proof._provider(dep,provider)
    claims={'sub':'stable-subject','nonce':nonce,'iss':config['issuer'],'email':'member@client.org',
        'email_verified':True,'hd':'client.org','tid':tenant,'oid':'stable-tenant-object'}
    monkeypatch.setattr(access_proof,'validate_id_token',lambda *a:claims)
    with pytest.raises(access.AccessDenied):finish_oidc(db,deployment_id=dep.id,provider=provider,state=state,code='auth-code',browser='forged')
    result=finish_oidc(db,deployment_id=dep.id,provider=provider,state=state,code='auth-code',browser=browser)
    if provider=='microsoft':
        assert 'handoff' not in result  # email claim alone cannot grant access
        raw=access_proof.finish_email(db,deployment_id=dep.id,raw=result['challenge'],browser=browser)
    else:raw=result['handoff']
    assert raw.startswith('ly')
    with pytest.raises(access.AccessDenied):finish_oidc(db,deployment_id=dep.id,provider=provider,state=state,code='auth-code',browser=browser)


@pytest.mark.parametrize('claim,value',[('nonce','forged'),('email_verified',False),('hd','other.org'),('email','other@client.org'),('sub',''),('azp','another-client')])
def test_oidc_claim_substitution_never_grants(db,monkeypatch,claim,value):
    from urllib.parse import urlsplit,parse_qs
    monkeypatch.setattr(access.settings,'GOOGLE_CLIENT_ID','google-client')
    monkeypatch.setattr(access.settings,'GOOGLE_CLIENT_SECRET','google-secret')
    monkeypatch.setattr(access.settings,'ENVIRONMENT','prod')
    dep=deployment(db,environment='production');dep.access_policy={'allowed_domains':['client.org'],'google_domain':'client.org'};db.flush();roster(db,dep,['member@client.org'])
    query=parse_qs(urlsplit(access_proof.begin_oidc(db,deployment_id=dep.id,provider='google',browser='b'*43)).query)
    claims={'sub':'stable','nonce':query['nonce'][0],'iss':'https://accounts.google.com','email':'member@client.org','email_verified':True,'hd':'client.org'}
    claims[claim]=value
    monkeypatch.setattr(access_proof,'exchange_code',lambda *a:'signed-token')
    monkeypatch.setattr(access_proof,'validate_id_token',lambda *a:claims)
    with pytest.raises(access.AccessDenied):finish_oidc(db,deployment_id=dep.id,provider='google',state=query['state'][0],code='code',browser='b'*43)
    assert db.query(AccessEntitlement).filter_by(deployment_id=dep.id).count()==0


def test_issue_revoke_and_roster_confirm_concurrency(engine):
    with Session(engine) as db:
        dep=deployment(db);ent=access.founder_grant(db,deployment_id=dep.id,operator='founder',label='issuance-race')
        dep_id,ent_id=dep.id,ent.id;db.commit()
    def issue():
        with Session(engine) as db:
            try:raw=access.issue_handoff(db,ent_id);db.commit();return raw
            except access.AccessDenied:return None
    def revoke():
        with Session(engine) as db:access.revoke_entitlement(db,entitlement_id=ent_id);db.commit()
    with ThreadPoolExecutor(max_workers=2) as pool:
        issued=pool.submit(issue);revoked=pool.submit(revoke);raw=issued.result();revoked.result()
    with Session(engine) as db:
        if raw:
            with pytest.raises(access.AccessDenied):access.redeem_handoff(db,raw,actor())
        assert db.get(AccessEntitlement,ent_id).revoked_at
    with Session(engine) as db:
        first=access.preview_roster(db,deployment_id=dep_id,mode='full_snapshot',rows=[{'email':'a@client.org','action':'add'}],expected_total=1,source_ref='first',operator='founder')
        second=access.preview_roster(db,deployment_id=dep_id,mode='full_snapshot',rows=[{'email':'b@client.org','action':'add'}],expected_total=1,source_ref='second',operator='founder');db.commit()
    def confirm(preview):
        with Session(engine) as db:
            try:
                access.apply_roster(db,deployment_id=dep_id,version_id=preview.version_id,confirmation=preview.confirmation,counts=preview.counts,operator='founder');db.commit();return True
            except access.AccessDenied:return False
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(confirm,[first,second]))
    assert sorted(results)==[False,True]


async def test_returning_telegram_start_and_revoked_text_do_not_repeat_sso(db,monkeypatch):
    from unittest.mock import AsyncMock
    from app import telegram
    dep=deployment(db)
    entitlement=access.founder_grant(db,deployment_id=dep.id,operator='founder',label='returning')
    tg=actor();user,_=access.redeem_handoff(db,access.issue_handoff(db,entitlement.id),tg)
    @contextmanager
    def session():yield db
    monkeypatch.setattr(telegram,'SessionLocal',session)
    message=SimpleNamespace(text='/start',from_user=tg,chat=SimpleNamespace(id=tg.id,type='private'),answer=AsyncMock())
    before=db.query(User).count()
    await telegram.cmd_start(message)
    assert message.answer.call_args.args[0]=='З поверненням! Продовжуємо.'
    assert db.query(User).count()==before
    access.revoke_entitlement(db,entitlement_id=entitlement.id)
    await telegram.cmd_start(message)
    assert 'Доступ зараз недоступний' in message.answer.call_args.args[0]


def test_same_identity_regrant_preserves_account_and_ends_old_enrollment(db,monkeypatch):
    monkeypatch.setattr(access.settings,'ENVIRONMENT','prod')
    dep=deployment(db,environment='production');tg=actor()
    user,_=access.redeem_handoff(db,verified(db,dep),tg)
    old=access.require_access(db,user.id)
    access.revoke_entitlement(db,entitlement_id=old.entitlement_id)
    roster(db,dep,['member@client.org'])  # New explicit restoration decision.
    raw=access_proof.begin_email(db,deployment_id=dep.id,email='member@client.org',browser='b'*43)
    handoff=access_proof.finish_email(db,deployment_id=dep.id,raw=raw,browser='b'*43)
    same,created=access.redeem_handoff(db,handoff,tg)
    assert same.id==user.id and not created and old.ended_at
    assert access.require_access(db,user.id).entitlement_id!=old.entitlement_id


def test_real_redis_rate_limit_is_shared_atomic_and_expires():
    import redis
    subject='wp041-fixture:'+uuid4().hex
    access_proof.rate_limit(subject,limit=1,window=60)
    with pytest.raises(access_proof.RateLimited):access_proof.rate_limit(subject,limit=1,window=60)
    client=redis.Redis.from_url(access.settings.REDIS_URL)
    key=f'ly:{access.settings.DEPLOYMENT_ID}:access-rate:'+access.digest(subject,'rate')
    assert 0 < client.ttl(key) <= 60
    client.delete(key)


@pytest.mark.parametrize('mode',['single','distributed'])
def test_legacy_implicit_kyiv_is_not_an_explicit_override(db,mode):
    user=User(tg_id=actor().id,timezone_override='Europe/Kyiv',timezone_override_explicit=False)
    db.add(user);db.flush()
    dep=deployment(db,mode=mode,zone='America/New_York' if mode=='single' else None)
    ent=access.founder_grant(db,deployment_id=dep.id,operator='founder',label='legacy-'+mode)
    access.redeem_handoff(db,access.issue_handoff(db,ent.id),SimpleNamespace(id=user.tg_id,username=None,first_name=None))
    assert user.timezone_override=='Europe/Kyiv'  # Preserve unknown legacy evidence.
    assert user.timezone==('America/New_York' if mode=='single' else None)
    user.timezone='Europe/Paris';db.flush();db.refresh(user)
    assert user.timezone_override_explicit and user.timezone=='Europe/Paris'


def test_event_and_aggregate_capture_enrollment_and_replay_original_snapshot(db):
    from app.telemetry import write_event_operation
    dep=deployment(db);user=User(tg_id=actor().id,timezone='UTC');db.add(user);db.flush()
    old=enroll(db,user,target=dep)
    first=write_event_operation(db,user_id=user.id,event_name='user_message',event_source='telegram',source_operation_id='attribution:'+uuid4().hex,properties={'message_length':1})
    assert first.event.deployment_enrollment_id==old.id
    assert first.contribution.dimensions['deployment_id']==dep.id
    old.ended_at=access.now_utc();db.flush();enroll(db,user,enrolled_at=access.now_utc())
    again=write_event_operation(db,user_id=user.id,event_name='user_message',event_source='telegram',source_operation_id=first.event.source_operation_id,properties={'message_length':1})
    assert again.duplicate and again.event.deployment_id==dep.id


@pytest.mark.parametrize('change',['commercial-end','revoked','replacement'])
def test_late_actual_exercise_receipt_keeps_claim_attribution(db,change):
    from app.content_library import load_content_library
    from app.db import UserEvent,AggregateRecord
    from app.scheduled_delivery import claim_send,finish_send
    from tests.test_wp_03_3_postgres_presentation import active_step
    from tests.test_wp_03_4_postgres_delivery import success
    load_content_library(db)
    user,plan,step=active_step(db)
    membership=access.require_access(db,user.id);dep=db.get(Deployment,membership.deployment_id)
    claim=claim_send(db,step.id)
    if change=='commercial-end':dep.ends_at=access.now_utc()
    elif change=='revoked':access.revoke_entitlement(db,entitlement_id=membership.entitlement_id)
    else:
        membership.ended_at=access.now_utc();db.flush();enroll(db,user,enrolled_at=access.now_utc())
    db.flush();actual=access.now_utc()
    finish_send(db,claim.attempt_id,success(claim),now=actual);db.flush()
    event=db.query(UserEvent).filter_by(plan_step_id=step.id,event_name='task_delivered').one()
    contribution=db.query(AggregateRecord).filter_by(source_operation_id=event.source_operation_id).one()
    assert event.occurred_at==actual and event.attribution_at<actual
    assert event.deployment_id==dep.id and event.deployment_enrollment_id==membership.id
    assert contribution.dimensions['deployment_id']==dep.id
    assert step.step_status=='delivered'


def test_manual_revoke_cannot_be_undone_by_repeating_identity_proof(db,monkeypatch):
    monkeypatch.setattr(access.settings,'ENVIRONMENT','prod')
    dep=deployment(db,environment='production');raw=verified(db,dep)
    entitlement=db.query(AccessEntitlement).filter_by(deployment_id=dep.id).one()
    access.revoke_entitlement(db,entitlement_id=entitlement.id)
    code=access_proof.begin_email(db,deployment_id=dep.id,email='member@client.org',browser='b'*43)
    with pytest.raises(access.AccessDenied):access_proof.finish_email(db,deployment_id=dep.id,raw=code,browser='b'*43)
    with pytest.raises(access.AccessDenied):access.redeem_handoff(db,raw,actor())
    assert db.query(AccessEntitlement).filter_by(deployment_id=dep.id,revoked_at=None).count()==0
    roster(db,dep,['member@client.org','other@client.org'],mode='delta',rows=[{'email':'other@client.org','action':'add'}])
    code=access_proof.begin_email(db,deployment_id=dep.id,email='member@client.org',browser='b'*43)
    with pytest.raises(access.AccessDenied):access_proof.finish_email(db,deployment_id=dep.id,raw=code,browser='b'*43)
    roster(db,dep,['member@client.org','other@client.org'],mode='delta',rows=[{'email':'member@client.org','action':'add'}])
    code=access_proof.begin_email(db,deployment_id=dep.id,email='member@client.org',browser='b'*43)
    assert access_proof.finish_email(db,deployment_id=dep.id,raw=code,browser='b'*43).startswith('ly')


def test_slow_oidc_exchange_does_not_block_revocation_or_reuse_state(engine,monkeypatch):
    from threading import Event
    from urllib.parse import urlsplit,parse_qs
    from sqlalchemy.orm import sessionmaker
    monkeypatch.setattr(access.settings,'ENVIRONMENT','prod')
    monkeypatch.setattr(access.settings,'GOOGLE_CLIENT_ID','google-client')
    monkeypatch.setattr(access.settings,'GOOGLE_CLIENT_SECRET','google-secret')
    sessions=sessionmaker(bind=engine,expire_on_commit=False)
    with sessions() as db:
        dep=deployment(db,environment='production');dep.access_policy={'allowed_domains':['client.org'],'google_domain':'client.org'};db.flush()
        roster(db,dep,['network@client.org'])
        ent=access.grant_verified(db,dep.id,access.email_digest('network@client.org'))
        dep_id,ent_id=dep.id,ent.id
        query=parse_qs(urlsplit(access_proof.begin_oidc(db,deployment_id=dep.id,provider='google',browser='b'*43)).query);db.commit()
    entered,released=Event(),Event()
    def exchange(*args):
        entered.set();assert released.wait(5);raise access.AccessDenied()
    monkeypatch.setattr(access_proof,'exchange_code',exchange)
    def callback():
        with sessions() as db:
            prepared=access_proof.prepare_oidc(db,deployment_id=dep_id,provider='google',state=query['state'][0],browser='b'*43);db.commit()
        with pytest.raises(access.AccessDenied):access_proof.verify_oidc(prepared,state=query['state'][0],code='code')
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(callback);assert entered.wait(3)
        try:
            with sessions() as db:
                db.execute(text("SET LOCAL lock_timeout='500ms'"))
                access.revoke_entitlement(db,entitlement_id=ent_id);db.commit()
        finally:released.set()
        future.result()
    with sessions() as db:
        with pytest.raises(access.AccessDenied):access_proof.prepare_oidc(db,deployment_id=dep_id,provider='google',state=query['state'][0],browser='b'*43)
        assert db.get(AccessEntitlement,ent_id).revoked_at


@pytest.mark.parametrize('change',['commercial-end','same-second-new-membership'])
def test_completion_receipt_keeps_original_send_claim(db,monkeypatch,change):
    from app.content_library import load_content_library
    from app.db import PlanCompletion,UserEvent,AggregateRecord
    from app.plan_completion.workflow import claim_report,finish_report
    from tests.test_wp_03_5_postgres_completion import source_plan,terminalize,close
    load_content_library(db)
    user,plan=source_plan(db)
    terminalize(db,user,plan)
    close(db,user,plan)
    membership=access.require_access(db,user.id);dep=db.get(Deployment,membership.deployment_id)
    claim_at=(access.now_utc()+timedelta(seconds=1)).replace(microsecond=800000)
    monkeypatch.setattr(access,'now_utc',lambda:claim_at)
    from app import telemetry
    monkeypatch.setattr(telemetry,'_utc_now',lambda:claim_at+timedelta(seconds=2))
    if change=='same-second-new-membership':
        membership.ended_at=claim_at-timedelta(seconds=1);db.flush()
        membership=enroll(db,user,target=dep,enrolled_at=claim_at-timedelta(microseconds=100000))
        dep.starts_at=membership.enrolled_at;db.flush()
    claim=claim_report(db,user.id,plan.id,now=claim_at)
    assert claim is not None
    if change=='commercial-end':
        dep.ends_at=claim_at+timedelta(microseconds=1);db.flush()
        actual=claim_at+timedelta(seconds=1)
    else:
        actual=claim_at.replace(microsecond=0)
    finish_report(db,user.id,claim,outcome='sent',message_id=12345,now=actual)
    receipt=db.get(PlanCompletion,plan.id)
    event=db.query(UserEvent).filter_by(plan_id=plan.id,event_name='plan_completion_sent').one()
    assert receipt.sent_at==event.occurred_at==actual
    assert event.attribution_at==receipt.started_at
    assert event.deployment_id==dep.id
    assert event.deployment_enrollment_id==membership.id
    contribution=db.query(AggregateRecord).filter_by(source_operation_id=event.source_operation_id).one()
    assert contribution.dimensions['deployment_id']==dep.id


@pytest.mark.parametrize('name,gap',[('task_delivered',1),('task_delivered',2),('user_message',0.5),('user_message',-0.5)])
def test_attribution_precision_bound_is_enforced_by_writer_and_database(db,name,gap):
    from app.content_library import load_content_library
    from app.db import UserEvent
    from app.telemetry import write_event_operation,EventValidationError
    from app.scheduled_delivery import claim_send,finish_send
    from sqlalchemy.exc import IntegrityError
    from tests.test_wp_03_3_postgres_presentation import active_step
    from tests.test_wp_03_4_postgres_delivery import success
    load_content_library(db)
    user,_,step=active_step(db)
    if name=='task_delivered':
        claim=claim_send(db,step.id);finish_send(db,claim.attempt_id,success(claim))
        original=db.query(UserEvent).filter_by(plan_step_id=step.id,event_name=name).one()
    else:
        original=write_event_operation(db,user_id=user.id,event_name=name,event_source='telegram',
            source_operation_id='telegram:precision:'+uuid4().hex,properties={'message_length':1}).event
    invalid_at=original.occurred_at+timedelta(seconds=gap)
    with pytest.raises(EventValidationError):
        write_event_operation(db,user_id=user.id,event_name=name,event_source=original.event_source,
            source_operation_id=original.event_source+':invalid:'+uuid4().hex,properties=original.properties,
            plan_step_id=original.plan_step_id,occurred_at=original.occurred_at,attribution_at=invalid_at)
    values={column.name:getattr(original,column.name) for column in UserEvent.__table__.columns if column.name!='event_id'}
    values.update(attribution_at=invalid_at,source_operation_id=original.event_source+':direct:'+uuid4().hex)
    with pytest.raises(IntegrityError,match='ck_event_attribution_time'),db.begin_nested():
        db.add(UserEvent(**values));db.flush()


@pytest.mark.parametrize('state,delivery,allowed',[
    ('active',False,True),('no-enrollment',False,False),('revoked',False,False),
    ('commercial-end',False,False),('wrong-environment',False,False),
    ('ended',False,False),('inactive',False,False),
    ('delivery-off',False,True),('delivery-off',True,False),('enrollment-closed',False,True),
])
def test_final_outbound_snapshot_is_one_current_authority_query(engine,monkeypatch,state,delivery,allowed):
    from sqlalchemy import event
    from sqlalchemy.orm import sessionmaker
    from app import db as models
    sessions=sessionmaker(bind=engine,expire_on_commit=False)
    monkeypatch.setattr(models,'SessionLocal',sessions)
    with sessions() as db:
        dep=deployment(db,environment='production' if state=='wrong-environment' else 'testnet')
        user=User(tg_id=actor().id,timezone='UTC');db.add(user);db.flush()
        membership=None if state=='no-enrollment' else enroll(db,user,target=dep)
        if state=='revoked':access.revoke_entitlement(db,entitlement_id=membership.entitlement_id)
        if state=='commercial-end':dep.ends_at=access.now_utc()-timedelta(seconds=1)
        if state=='ended':membership.ended_at=access.now_utc()
        if state=='inactive':user.is_active=False
        if state=='delivery-off':dep.delivery_enabled=False
        if state=='enrollment-closed':dep.enrollment_open=False
        db.commit();user_id=user.id
    statements=[]
    def track(conn,cursor,statement,*args):statements.append(statement)
    event.listen(engine,'before_cursor_execute',track)
    try:
        assert (access.outbound_claim(user_id,delivery=delivery) is not None)==allowed
    finally:event.remove(engine,'before_cursor_execute',track)
    assert len(statements)==1 and 'FOR UPDATE' not in statements[0]


def test_final_outbound_snapshot_does_not_queue_on_another_company_reader(engine,monkeypatch):
    from sqlalchemy.orm import sessionmaker
    from app import db as models
    sessions=sessionmaker(bind=engine,expire_on_commit=False)
    monkeypatch.setattr(models,'SessionLocal',sessions)
    with sessions() as db:
        dep=deployment(db);user=User(tg_id=actor().id,timezone='UTC');db.add(user);db.flush()
        enroll(db,user,target=dep);db.commit();user_id,dep_id=user.id,dep.id
    with engine.begin() as holder:
        holder.execute(text('SELECT id FROM deployments WHERE id=:id FOR UPDATE'),{'id':dep_id})
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(access.outbound_claim,user_id)
            try:
                assert future.result(timeout=1) is not None
            finally:
                holder.rollback()  # Always release before joining a failed worker.

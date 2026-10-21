"""Adversarial caller proof, perimeter, isolation and provider claim checks."""
import hashlib
import hmac
import json
import asyncio
import importlib
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from app import access, access_proof, access_environment, api
from app.config import Settings, ConfigurationError


def webapp(actor=77, *, seconds_ago=0, token=None, extra=None):
    data={'auth_date':str(int((access.now_utc()-timedelta(seconds=seconds_ago)).timestamp())), 'user':json.dumps({'id':actor})}
    data.update(extra or {})
    secret=hmac.new(b'WebAppData',(token or access.settings.BOT_TOKEN).encode(),hashlib.sha256).digest()
    data['hash']=hmac.new(secret,'\n'.join(f'{k}={v}' for k,v in sorted(data.items())).encode(),hashlib.sha256).hexdigest()
    return urlencode(data)


def test_webapp_valid_owner_identity():
    assert access_proof.verify_webapp(webapp(123))==123


@pytest.mark.parametrize('raw', ['', 'forged', webapp(seconds_ago=301), webapp(seconds_ago=-60),webapp(token='another-bot'),webapp(actor='123'),webapp(actor=-1),webapp(actor=True), webapp()+'&auth_date=1'])
def test_webapp_forged_expired_duplicate_or_cross_bot_rejected(raw):
    with pytest.raises(access.AccessDenied,match='^access_unavailable$'):access_proof.verify_webapp(raw)


@pytest.mark.parametrize('proof',['','forged',webapp(seconds_ago=600),webapp(token='wrong-environment-bot')])
def test_http_denies_before_db_and_does_not_enumerate(monkeypatch,proof):
    monkeypatch.setattr(api,'SessionLocal',lambda:pytest.fail('invalid proof must not open database'))
    # Session is opened but validation is before target query. Stub the context.
    from contextlib import nullcontext
    monkeypatch.setattr(api,'SessionLocal',lambda:nullcontext(object()))
    client=TestClient(api.app)
    responses=[client.post('/user/time-slots',params={'user_id':target},json={'DAY':'14:00'},headers={'Idempotency-Key':'not-authentication','X-Telegram-Init-Data':proof}) for target in (1,999999)]
    assert [r.status_code for r in responses]==[403,403]
    assert responses[0].json()==responses[1].json()=={'detail':'access_unavailable'}


def test_http_rate_limit_is_neutral_before_target_lookup(monkeypatch):
    from contextlib import nullcontext
    monkeypatch.setattr(api,'SessionLocal',lambda:nullcontext(object()))
    def exhausted(*args,**kwargs):raise access_proof.RateLimited()
    monkeypatch.setattr(access_proof,'rate_limit',exhausted)
    response=TestClient(api.app).post('/user/time-slots',params={'user_id':987},json={'DAY':'14:00'},headers={'X-Telegram-Init-Data':webapp(),'Idempotency-Key':'stable'})
    assert response.status_code==429 and response.json()=={'detail':'access_unavailable'}


def test_enrollment_page_csrf_headers_and_no_raw_credentials():
    client=TestClient(api.app)
    page=client.get('/enroll/123')
    assert page.status_code==200
    assert page.headers['cache-control']=='no-store'
    assert page.headers['referrer-policy']=='no-referrer'
    assert 'HttpOnly' in page.headers['set-cookie'] and 'SameSite=lax' in page.headers['set-cookie']
    assert access.settings.ACCESS_TOKEN_SECRET not in page.text
    result=client.post('/enroll/123/email',data={'email':'private@client.org','csrf':'forged'},headers={'origin':access.settings.APP_BASE_URL})
    assert result.status_code==403 and 'private@client.org' not in result.text
    result=client.get('/enroll/123/oidc/google/callback?state=secret&code=secret')
    assert result.status_code==403 and 'secret' not in result.text


def test_operator_routes_reject_unauthenticated_request_before_db(monkeypatch):
    monkeypatch.setattr(api,'SessionLocal',lambda:pytest.fail('no anonymous admin DB'))
    client=TestClient(api.app)
    for route in ('roster/preview','roster/apply','testnet/handoff','revoke'):
        response=client.post('/access/'+route,json={'email':'private@client.org'})
        assert response.status_code==403 and response.json()=={'detail':'access_unavailable'}


@pytest.mark.parametrize('enabled', [False, True])
def test_delivery_control_wakes_only_after_commit_and_retries_when_enabled(monkeypatch, enabled):
    from app import access_http, scheduler
    calls = []
    class DB:
        def commit(self): calls.append('commit')
    monkeypatch.setattr(access_http, '_operator', lambda request: 'founder')
    monkeypatch.setattr(access_http, 'SessionLocal', lambda: nullcontext(DB()))
    monkeypatch.setattr(access, 'set_deployment_controls',
        lambda db, **kwargs: calls.append('controls') or enabled)
    monkeypatch.setattr(scheduler, 'wake_scheduled_delivery_recovery',
        lambda: calls.append('wake'))
    response = TestClient(api.app).post('/access/deployment/controls',
        json={'deployment_id': 17, 'enrollment_open': True, 'delivery_enabled': enabled})
    assert response.status_code == 200
    assert calls == (['controls', 'commit', 'wake'] if enabled else ['controls', 'commit'])


def inventory(monkeypatch):
    actual=access_environment.resource_fingerprints()
    profile={'runtime_id':access.settings.DEPLOYMENT_ID,'resources':actual,
        'openai_project':access.settings.OPENAI_PROJECT_ID or 'dev-project','backup_scope':'test-backups','alert_route':'test-alerts','aggregate_sink':'test-db'}
    other={'runtime_id':'other','resources':{k:'other-'+v for k,v in actual.items()},
        'openai_project':'other-project','backup_scope':'other-backups','alert_route':'other-alerts','aggregate_sink':'other-db'}
    value={'testnet':profile,'production':other}
    monkeypatch.setattr(access.settings,'ACCESS_ISOLATION_INVENTORY',json.dumps(value))
    return value


@pytest.mark.parametrize('allowed', [False, True])
def test_silent_send_claims_current_delivery_access_and_attributes_at_claim(monkeypatch, allowed):
    from app import scheduler
    now = access.now_utc()
    user = SimpleNamespace(id=77, tg_id=123, first_name='Member', profile=SimpleNamespace(coach_persona='empath'))
    last_event = SimpleNamespace(occurred_at=now - timedelta(days=3))
    class Query:
        def __init__(self, value): self.value = value
        def filter(self, *args): return self
        def order_by(self, *args): return self
        def first(self): return self.value
        def all(self): return [user]
    class DB:
        lookups = 0
        def query(self, model):
            self.lookups += 1
            return Query(last_event if self.lookups == 1 else None)
        def commit(self): pass
    monkeypatch.setattr(scheduler, 'SessionLocal', lambda: nullcontext(DB()))
    monkeypatch.setattr(scheduler, '_active_plan_users_query', lambda db: Query(None))
    monkeypatch.setattr(scheduler, 'can_send_auto_message', lambda *args: True)
    monkeypatch.setattr(scheduler, 'get_trigger_message', lambda *args: 'Reminder')
    claim_calls = []
    monkeypatch.setattr(scheduler, 'outbound_claim', lambda uid, *, delivery: claim_calls.append((uid, delivery)) or (now if allowed else None))
    sent = []
    async def send(*args):
        sent.append(args)
        return object()
    class Future:
        def result(self, timeout): return asyncio.run(self.coro)
        def __init__(self, coro): self.coro = coro
    monkeypatch.setattr(scheduler, '_send_message_async', send)
    monkeypatch.setattr(scheduler, '_submit_coroutine', Future)
    events = []
    attempts = []
    monkeypatch.setattr(scheduler, '_record_silent_attempt', lambda *args: attempts.append(args) or True)
    monkeypatch.setattr(scheduler, '_record_silent_receipt', lambda *args: events.append(args))

    scheduler.check_silent_users()

    assert claim_calls == [(77, True)]
    assert len(attempts) == 1
    assert sent == ([(123, 'Reminder')] if allowed else [])
    assert len(events) == int(allowed)
    if allowed:
        assert events[0][-1] == now


def test_queued_silent_reminder_rechecks_after_access_is_revoked(monkeypatch):
    from app import scheduler
    now = access.now_utc()
    user = SimpleNamespace(id=77, tg_id=123, first_name='Member', profile=SimpleNamespace(coach_persona='empath'))
    last_event = SimpleNamespace(occurred_at=now - timedelta(days=3))
    class Query:
        def __init__(self, value): self.value = value
        def filter(self, *args): return self
        def order_by(self, *args): return self
        def first(self): return self.value
        def all(self): return [user]
    class DB:
        def __init__(self): self.lookups = 0
        def query(self, model):
            self.lookups += 1
            return Query(last_event if self.lookups == 1 else None)
    monkeypatch.setattr(scheduler, 'SessionLocal', lambda: nullcontext(DB()))
    monkeypatch.setattr(scheduler, '_active_plan_users_query', lambda db: Query(None))
    monkeypatch.setattr(scheduler, 'can_send_auto_message', lambda *args: True)
    monkeypatch.setattr(scheduler, 'get_trigger_message', lambda *args: 'Reminder')
    monkeypatch.setattr(scheduler, '_record_silent_attempt', lambda *args: True)
    claims = []
    revoked = False
    def claim(*args, **kwargs):
        claims.append(revoked)
        return None if revoked else now
    monkeypatch.setattr(scheduler, 'outbound_claim', claim)
    sent = []
    async def send(*args): sent.append(args); return object()
    monkeypatch.setattr(scheduler, '_send_message_async', send)
    monkeypatch.setattr(scheduler, '_record_silent_receipt', lambda *args: pytest.fail('denied send cannot have receipt'))
    queued = []
    class Future:
        def __init__(self, coro): queued.append(coro)
        def result(self, timeout): raise TimeoutError()
    monkeypatch.setattr(scheduler, '_submit_coroutine', Future)

    scheduler.check_silent_users()
    revoked = True
    assert asyncio.run(queued.pop()) is False
    assert claims == [True] and sent == []


@pytest.mark.parametrize('confirmed', [False, True])
def test_silent_timeout_keeps_confirmed_receipt_or_unknown_attempt_fence(monkeypatch, confirmed):
    from app import scheduler
    now = access.now_utc()
    user = SimpleNamespace(id=77, tg_id=123, first_name='Member', profile=SimpleNamespace(coach_persona='empath'))
    last_event = SimpleNamespace(occurred_at=now - timedelta(days=3))
    attempts, receipts, queued = [], [], []
    class Query:
        def __init__(self, value): self.value = value
        def filter(self, *args): return self
        def order_by(self, *args): return self
        def first(self): return self.value
        def all(self): return [user]
    class DB:
        def __init__(self): self.lookups = 0
        def query(self, model):
            self.lookups += 1
            value = last_event if self.lookups == 1 else (SimpleNamespace(occurred_at=now) if attempts else None)
            return Query(value)
    monkeypatch.setattr(scheduler, 'SessionLocal', lambda: nullcontext(DB()))
    monkeypatch.setattr(scheduler, '_active_plan_users_query', lambda db: Query(None))
    monkeypatch.setattr(scheduler, 'can_send_auto_message', lambda *args: True)
    monkeypatch.setattr(scheduler, 'get_trigger_message', lambda *args: 'Reminder')
    monkeypatch.setattr(scheduler, '_record_silent_attempt', lambda *args: attempts.append(args) or True)
    monkeypatch.setattr(scheduler, 'outbound_claim', lambda *args, **kwargs: now)
    async def send(*args): return object() if confirmed else None
    monkeypatch.setattr(scheduler, '_send_message_async', send)
    monkeypatch.setattr(scheduler, '_record_silent_receipt', lambda *args: receipts.append(args))
    class Future:
        def __init__(self, coro): queued.append(coro)
        def result(self, timeout): raise TimeoutError()
    monkeypatch.setattr(scheduler, '_submit_coroutine', Future)

    scheduler.check_silent_users()  # The 30-second waiter expires; coroutine still owns the send.
    assert not receipts and len(queued) == 1
    assert asyncio.run(queued.pop()) is confirmed
    assert len(receipts) == int(confirmed)
    if confirmed:
        assert receipts[0][-1] == now
    scheduler.check_silent_users()  # A later scan sees the attempt even if send stayed unknown.
    assert len(queued) == 0 and len(attempts) == 1


def test_silent_attempt_and_receipt_use_distinct_idempotency_operations(monkeypatch):
    from app import scheduler
    operations = []
    class DB:
        def commit(self): pass
    monkeypatch.setattr(scheduler, 'SessionLocal', lambda: nullcontext(DB()))
    def attempt(db, **kwargs):
        operations.append(kwargs['source_operation_id'])
        return SimpleNamespace(duplicate=False)
    def receipt(db, **kwargs):
        operations.append(kwargs['source_operation_id'])
    monkeypatch.setattr(scheduler, 'write_event_operation', attempt)
    monkeypatch.setattr(scheduler, 'log_user_event', receipt)
    claim_at = access.now_utc()

    assert scheduler._record_silent_attempt(77, 'silent_2_days', 3, 'scheduler:silent:77:day')
    scheduler._record_silent_receipt(77, 'silent_2_days', 3, 'scheduler:silent:77:day', claim_at)

    assert operations == ['scheduler:silent:77:day:attempt', 'scheduler:silent:77:day:sent']


@pytest.mark.asyncio
@pytest.mark.parametrize('is_dev', [True, False])
async def test_runtime_startup_audits_bound_contours_only(monkeypatch, is_dev):
    main = importlib.import_module('app.main')
    monkeypatch.setattr(main.settings, 'IS_DEV', is_dev)
    if is_dev:
        monkeypatch.setattr(main.settings, 'REDIS_URL', '')
        monkeypatch.setattr(main.settings, 'ACCESS_ISOLATION_INVENTORY', '')
    monkeypatch.setattr(main, 'audit_startup_schema', lambda: None)
    db = object()
    monkeypatch.setattr('app.db.SessionLocal', lambda: nullcontext(db))
    audit_calls = []
    monkeypatch.setattr('app.access_environment.audit_environment', lambda session: audit_calls.append(session))
    async def finished(*args): pass
    monkeypatch.setattr(main, 'schedule_daily_loop', finished)
    monkeypatch.setattr(main.dp, 'start_polling', finished)
    monkeypatch.setattr(main.uvicorn, 'Config', lambda *args, **kwargs: object())
    monkeypatch.setattr(main.uvicorn, 'Server', lambda config: SimpleNamespace(serve=finished))

    await main.main()

    assert audit_calls == ([] if is_dev else [db])


@pytest.mark.asyncio
async def test_bound_runtime_stops_before_polling_when_isolation_audit_fails(monkeypatch):
    main = importlib.import_module('app.main')
    monkeypatch.setattr(main.settings, 'IS_DEV', False)
    monkeypatch.setattr(main, 'audit_startup_schema', lambda: None)
    monkeypatch.setattr('app.db.SessionLocal', lambda: nullcontext(object()))
    def rejected(db): raise access.AccessDenied()
    monkeypatch.setattr('app.access_environment.audit_environment', rejected)
    monkeypatch.setattr(main, 'schedule_daily_loop', lambda: pytest.fail('no scheduler before isolation proof'))

    with pytest.raises(access.AccessDenied):
        await main.main()


def test_isolation_inventory_matches_runtime(monkeypatch):
    inventory(monkeypatch)
    assert len(access_environment.inventory_digest())==64


@pytest.mark.parametrize('resource', ['database','redis','telegram_bot','telegram_secret','openai_key','openai_project','access_key','operator_key','report_key','url','openai_project','backup_scope','alert_route','aggregate_sink','runtime_id'])
def test_cross_environment_resource_reuse_rejected(monkeypatch,resource):
    value=inventory(monkeypatch)
    if resource in value['testnet']['resources']:
        value['production']['resources'][resource]=value['testnet']['resources'][resource]
    else:value['production'][resource]=value['testnet'][resource]
    monkeypatch.setattr(access.settings,'ACCESS_ISOLATION_INVENTORY',json.dumps(value))
    with pytest.raises(access.AccessDenied):access_environment.inventory_digest()


def test_staging_label_alone_is_not_isolation_proof(monkeypatch):
    monkeypatch.setattr(access.settings,'ACCESS_ISOLATION_INVENTORY','{}')
    with pytest.raises(access.AccessDenied):access_environment.inventory_digest()


def test_rate_limit_atomic_expiry_and_failure(monkeypatch):
    class Redis:
        n=0
        def eval(self,script,count,key,window):
            assert 'EXPIRE' in script and count==1 and 'access-rate' in key
            self.n+=1;return self.n
    client=Redis();monkeypatch.setattr(access_proof.redis.Redis,'from_url',lambda *a,**kw:client)
    access_proof.rate_limit('same',limit=1)
    with pytest.raises(access_proof.RateLimited):access_proof.rate_limit('same',limit=1)
    def failed(*args,**kwargs):raise RuntimeError('private-cache-error')
    monkeypatch.setattr(access_proof.redis.Redis,'from_url',failed)
    with pytest.raises(access.AccessDenied,match='^access_unavailable$'):access_proof.rate_limit('same')


@pytest.mark.parametrize('case',['valid','legacy-valid','forged','issuer','audience','expired','missing-sub','algorithm'])
def test_oidc_real_signature_and_required_claims(monkeypatch,case):
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    at=int(access.now_utc().timestamp())
    claims={'iss':'https://accounts.google.com','aud':'our-client','sub':'subject','nonce':'nonce','iat':at,'exp':at+300}
    if case=='legacy-valid':claims['iss']='accounts.google.com'
    if case=='issuer':claims['iss']='https://attacker.invalid'
    if case=='audience':claims['aud']='another-client'
    if case=='expired':claims['exp']=at-1
    if case=='missing-sub':claims.pop('sub')
    signing=rsa.generate_private_key(public_exponent=65537,key_size=2048) if case=='forged' else key
    raw=jwt.encode(claims, 'attacker-key-for-tests-only-32-bytes' if case=='algorithm' else signing, algorithm='HS256' if case=='algorithm' else 'RS256',headers={'kid':'fixture-key'})
    class Client:
        def __init__(self,url,timeout):assert url=='https://www.googleapis.com/oauth2/v3/certs'
        def get_signing_key_from_jwt(self,raw):return SimpleNamespace(key=key.public_key())
    monkeypatch.setattr(jwt,'PyJWKClient',Client)
    config={'jwks':'https://www.googleapis.com/oauth2/v3/certs','issuer':access_proof.GOOGLE_ISSUERS,'client':'our-client'}
    if case in ('valid','legacy-valid'):assert access_proof.validate_id_token(config,raw)['sub']=='subject'
    else:
        with pytest.raises(access.AccessDenied):access_proof.validate_id_token(config,raw)


def test_microsoft_uppercase_tenant_uses_canonical_issuer(monkeypatch):
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    tenant='AABBCCDD-0011-2233-4455-66778899AABB'
    monkeypatch.setattr(access.settings,'MICROSOFT_CLIENT_ID','our-client')
    monkeypatch.setattr(access.settings,'MICROSOFT_CLIENT_SECRET','our-secret')
    config=access_proof._provider(SimpleNamespace(access_policy={'microsoft_tenant':tenant}),'microsoft')
    issuer=f'https://login.microsoftonline.com/{tenant.lower()}/v2.0'
    assert config['issuer']==issuer and tenant.lower() in config['authorize']
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    at=int(access.now_utc().timestamp())
    claims={'iss':issuer,'aud':'our-client','sub':'subject','tid':tenant.lower(),'nonce':'nonce','iat':at,'exp':at+300}
    raw=jwt.encode(claims,key,algorithm='RS256',headers={'kid':'fixture-key'})
    class Client:
        def __init__(self,url,timeout):assert tenant.lower() in url
        def get_signing_key_from_jwt(self,raw):return SimpleNamespace(key=key.public_key())
    monkeypatch.setattr(jwt,'PyJWKClient',Client)
    assert access_proof.validate_id_token(config,raw)['tid']==tenant.lower()


@pytest.mark.parametrize('text',['/start','/start invalid','/start newplan_SHORT_LITE_mixed'])
async def test_canonical_start_never_creates_unentitled_account(monkeypatch,text):
    from app import telegram
    from unittest.mock import AsyncMock
    from contextlib import nullcontext
    class Empty:
        def query(self,*a):return self
        def filter(self,*a):return self
        def with_for_update(self):return self
        def first(self):return None
        def add(self,*a):pytest.fail('unentitled Start created a record')
    monkeypatch.setattr(telegram,'SessionLocal',lambda:nullcontext(Empty()))
    message=SimpleNamespace(text=text,from_user=SimpleNamespace(id=77,username=None,first_name=None),chat=SimpleNamespace(type='private',id=77),answer=AsyncMock())
    await telegram.cmd_start(message)
    assert 'Доступ зараз недоступний' in message.answer.call_args.args[0]


def test_no_legacy_issuance_or_redemption_path():
    from app import telegram
    assert not hasattr(telegram,'_handle_newplan_deeplink')
    assert '?start=' not in api._deep_link({'duration':'SHORT','load':'LITE','focus':'mixed'})


async def test_revocation_during_coach_generation_denies_reply_and_history(monkeypatch):
    from unittest.mock import AsyncMock
    from app import orchestrator, telegram
    from app.lifecycle import CurrentMode
    class Memory:
        def __init__(self): self.messages = []
        async def append_message(self, user_id, role, text):
            self.messages.append((user_id, role, text))
    memory = Memory()
    async def no_completion(_user_id): return None
    monkeypatch.setattr(orchestrator, 'session_memory', memory)
    monkeypatch.setattr(telegram, 'session_memory', memory)
    monkeypatch.setattr(orchestrator, '_auto_complete_plan_if_needed_for_user_id', no_completion)
    monkeypatch.setattr(orchestrator, 'build_user_context', AsyncMock(return_value={'current_mode': CurrentMode.ACTIVE.value}))
    monkeypatch.setattr(orchestrator, 'coach_agent', AsyncMock(return_value={'reply_text': 'private generated reply'}))
    response = await orchestrator.handle_incoming_message(77, 'hello')
    assert memory.messages == [(77, 'user', 'hello')]
    monkeypatch.setattr(access,'outbound_claim',lambda *a,**k:None)
    monkeypatch.setattr(telegram,'SessionLocal',lambda:pytest.fail('denied reply must not be stored'))
    message=SimpleNamespace(answer=AsyncMock())
    await telegram._send_agent_response(message,77,response)
    message.answer.assert_awaited_once_with('Доступ зараз недоступний.')
    assert memory.messages == [(77, 'user', 'hello')]


async def test_assistant_memory_records_only_confirmed_telegram_reply(monkeypatch):
    from unittest.mock import AsyncMock
    from app import telegram
    class Memory:
        def __init__(self): self.messages = []
        async def append_message(self, user_id, role, text):
            self.messages.append((user_id, role, text))
    memory = Memory()
    monkeypatch.setattr(telegram, 'session_memory', memory)
    monkeypatch.setattr(access, 'outbound_claim', lambda *a, **k: access.now_utc())
    stored = []
    class DB:
        def add(self, row): stored.append(row)
        def commit(self): pass
    monkeypatch.setattr(telegram, 'SessionLocal', lambda: nullcontext(DB()))
    message = SimpleNamespace(answer=AsyncMock(side_effect=RuntimeError('telegram unavailable')))
    with pytest.raises(RuntimeError, match='telegram unavailable'):
        await telegram._send_agent_response(message, 77, {'reply_text': 'delivered reply'})
    assert memory.messages == [] and stored == []

    message.answer.side_effect = None
    await telegram._send_agent_response(message, 77, {'reply_text': 'delivered reply'})
    assert memory.messages == [(77, 'assistant', 'delivered reply')]
    assert len(stored) == 1 and stored[0].text == 'delivered reply'


async def test_revocation_during_daily_pulse_generation_prevents_send(monkeypatch):
    from contextlib import nullcontext
    from unittest.mock import AsyncMock
    from app import scheduler
    profile=SimpleNamespace(coach_persona='empath',pulse_sent_indices=[])
    user=SimpleNamespace(id=77,tg_id=123,profile=profile)
    class Query:
        def __init__(self,value):self.value=value
        def filter(self,*a):return self
        def all(self):return self.value
        def first(self):return self.value
    class DB:
        def query(self,model):return Query(user if model is scheduler.User else [])
        def commit(self):pytest.fail('denied pulse must not be saved')
    monkeypatch.setattr(scheduler,'SessionLocal',lambda:nullcontext(DB()))
    monkeypatch.setattr(scheduler,'_active_plan_users_query',lambda db:Query([user]))
    monkeypatch.setattr(scheduler,'_event_loop',object())
    scheduled=[]
    monkeypatch.setattr(scheduler.asyncio,'run_coroutine_threadsafe',lambda coro,loop:scheduled.append(coro))
    revoked=False
    async def generate(*args):
        nonlocal revoked
        revoked=True
        return 'private pulse',[1]
    monkeypatch.setattr(scheduler,'generate_pulse_message',generate)
    monkeypatch.setattr(scheduler,'outbound_claim',lambda *a,**k:None if revoked else access.now_utc())
    sender=AsyncMock();monkeypatch.setattr(scheduler,'_send_message_async',sender)
    scheduler.send_daily_pulse();await scheduled[0]
    sender.assert_not_awaited()
    assert profile.pulse_sent_indices==[]

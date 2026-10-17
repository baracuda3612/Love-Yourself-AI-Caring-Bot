"""Adversarial caller proof, perimeter, isolation and provider claim checks."""
import hashlib
import hmac
import json
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


def inventory(monkeypatch):
    actual=access_environment.resource_fingerprints()
    profile={'runtime_id':access.settings.DEPLOYMENT_ID,'resources':actual,
        'openai_project':access.settings.OPENAI_PROJECT_ID or 'dev-project','backup_scope':'test-backups','alert_route':'test-alerts','aggregate_sink':'test-db'}
    other={'runtime_id':'other','resources':{k:'other-'+v for k,v in actual.items()},
        'openai_project':'other-project','backup_scope':'other-backups','alert_route':'other-alerts','aggregate_sink':'other-db'}
    value={'testnet':profile,'production':other}
    monkeypatch.setattr(access.settings,'ACCESS_ISOLATION_INVENTORY',json.dumps(value))
    return value


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
    from app import telegram
    monkeypatch.setattr(access,'outbound_claim',lambda *a,**k:None)
    monkeypatch.setattr(telegram,'SessionLocal',lambda:pytest.fail('denied reply must not be stored'))
    message=SimpleNamespace(answer=AsyncMock())
    await telegram._send_agent_response(message,77,{'reply_text':'private generated reply'})
    message.answer.assert_awaited_once_with('Доступ зараз недоступний.')


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

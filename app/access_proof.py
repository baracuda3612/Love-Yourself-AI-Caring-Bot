"""Bounded identity proofs and caller authentication; no identity telemetry."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import smtplib
from datetime import timedelta
from email.message import EmailMessage
from urllib.parse import parse_qsl, urlencode
from urllib.request import Request, urlopen

import jwt
import redis

from app.access import (AccessDenied, PROOF_TTL, digest, email_digest, grant_verified,
    issue_handoff, lock_deployment, normalize_email, now_utc, require_access)
from app.config import settings
from app.db import AccessIdentity, AccessProof, User


def rate_limit(subject, *, limit=10, window=60):
    """Atomic fixed window. Cache failure denies; no worker-local fallback."""
    if not settings.REDIS_URL:
        raise AccessDenied()
    try:
        client = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=2, socket_timeout=2)
        key = f'ly:{settings.DEPLOYMENT_ID}:access-rate:' + digest(subject, 'rate')
        used = client.eval("local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],ARGV[1]) end; return n", 1, key, window)
    except Exception:
        raise AccessDenied() from None
    if used > limit:
        raise RateLimited()


class RateLimited(AccessDenied):
    pass


def verify_webapp(raw, *, now=None):
    try:
        if not isinstance(raw, str) or not 1 <= len(raw) <= 8192:
            raise AccessDenied()
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
        data = dict(pairs)
        if len(pairs) != len(data):
            raise AccessDenied()
        supplied = data.pop('hash')
        check = '\n'.join(f'{k}={v}' for k, v in sorted(data.items()))
        secret = hmac.new(b'WebAppData', settings.BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        at = (now or now_utc()).timestamp()
        if not hmac.compare_digest(supplied, expected) or not 0 <= at - int(data['auth_date']) <= 300:
            raise AccessDenied()
        actor = json.loads(data['user'])['id']
        if type(actor) is not int or actor <= 0:
            raise AccessDenied()
        return actor
    except (ValueError, KeyError, TypeError, OverflowError):
        raise AccessDenied() from None


def authorize_time_slots(db, raw, target_user_id):
    actor = verify_webapp(raw)
    rate_limit(f'webapp:{actor}', limit=20, window=60)
    user = db.query(User).filter(User.tg_id == actor).with_for_update().first()
    if user is None or user.id != target_user_id:
        raise AccessDenied()
    require_access(db, user.id)
    return user.id


def new_proof(db, *, deployment_id, kind, browser, identity_key=None, subject=None):
    deployment = lock_deployment(db, deployment_id, joining=True)
    if deployment.environment != 'production':
        raise AccessDenied()
    # Expired proof material has no audit/business value. Opportunistic bounded
    # retention needs no background recovery loop or additional scheduler owner.
    db.query(AccessProof).filter(AccessProof.expires_at < now_utc() - timedelta(days=1)).delete(synchronize_session=False)
    raw, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    at = now_utc()
    db.add(AccessProof(deployment_id=deployment_id, kind=kind,
        secret_digest=digest(raw, 'proof'), browser_digest=digest(browser, 'browser'),
        identity_digest=identity_key, subject_digest=subject, nonce_digest=digest(nonce, 'nonce'),
        issued_at=at, expires_at=at + PROOF_TTL))
    db.flush()
    return raw, nonce


def consume_proof(db, *, deployment_id, raw, browser, kind):
    lock_deployment(db, deployment_id, joining=True)
    proof = db.query(AccessProof).filter(AccessProof.secret_digest == digest(raw, 'proof'),
        AccessProof.deployment_id == deployment_id, AccessProof.kind == kind).populate_existing().with_for_update().first()
    at = now_utc()
    if (proof is None or proof.consumed_at or proof.issued_at > at or proof.expires_at <= at
        or not browser or not hmac.compare_digest(proof.browser_digest, digest(browser, 'browser'))):
        raise AccessDenied()
    proof.consumed_at = at
    db.flush()
    return proof


def begin_email(db, *, deployment_id, email, browser, subject=None):
    email = normalize_email(email)
    rate_limit('email:' + email, limit=3, window=600)
    # Store the same bounded proof for known and unknown addresses. Delivery
    # and public responses are identical; membership is checked only on redeem.
    raw, _ = new_proof(db, deployment_id=deployment_id, kind='email', browser=browser,
        identity_key=email_digest(email), subject=subject)
    return raw


def send_email(email, raw):
    if not all((settings.SMTP_HOST, settings.SMTP_FROM, settings.SMTP_USERNAME, settings.SMTP_PASSWORD)):
        raise AccessDenied()
    message = EmailMessage()
    message['From'] = settings.SMTP_FROM
    message['To'] = normalize_email(email)
    message['Subject'] = 'LY Workday — підтвердження адреси'
    message.set_content(f'Код для сторінки, де ти почав/почала вхід (10 хвилин):\n{raw}\nЯкщо ти не починав/починала вхід, проігноруй цей лист.')
    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as smtp:
        smtp.starttls()
        smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        smtp.send_message(message)


def finish_email(db, *, deployment_id, raw, browser):
    proof = consume_proof(db, deployment_id=deployment_id, raw=raw, browser=browser, kind='email')
    provider, subject = 'email', None
    if proof.subject_digest:
        provider, subject = proof.subject_digest.split(':', 1)
    entitlement = grant_verified(db, deployment_id, proof.identity_digest, provider=provider, subject_digest=subject)
    return issue_handoff(db, entitlement.id)


GOOGLE_ISSUERS = ('https://accounts.google.com', 'accounts.google.com')


def _provider(deployment, provider):
    if provider == 'google':
        if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET or not deployment.access_policy.get('google_domain'):
            raise AccessDenied()
        return dict(client=settings.GOOGLE_CLIENT_ID, secret=settings.GOOGLE_CLIENT_SECRET,
            authorize='https://accounts.google.com/o/oauth2/v2/auth',
            token='https://oauth2.googleapis.com/token', jwks='https://www.googleapis.com/oauth2/v3/certs',
            issuer=GOOGLE_ISSUERS)
    if provider == 'microsoft':
        tenant = deployment.access_policy.get('microsoft_tenant', '')
        import re
        if (not re.fullmatch(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}', tenant)
            or not settings.MICROSOFT_CLIENT_ID or not settings.MICROSOFT_CLIENT_SECRET):
            raise AccessDenied()
        root = f'https://login.microsoftonline.com/{tenant.lower()}'
        return dict(client=settings.MICROSOFT_CLIENT_ID, secret=settings.MICROSOFT_CLIENT_SECRET,
            authorize=root + '/oauth2/v2.0/authorize', token=root + '/oauth2/v2.0/token',
            jwks=root + '/discovery/v2.0/keys', issuer=root + '/v2.0')
    raise AccessDenied()


def redirect_uri(deployment_id, provider):
    return f'{settings.APP_BASE_URL}/enroll/{deployment_id}/oidc/{provider}/callback'


def begin_oidc(db, *, deployment_id, provider, browser):
    deployment = lock_deployment(db, deployment_id, joining=True)
    config = _provider(deployment, provider)
    state, nonce = new_proof(db, deployment_id=deployment_id, kind=provider, browser=browser)
    verifier = digest(state, 'pkce')
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    params = dict(client_id=config['client'], response_type='code', scope='openid email',
        redirect_uri=redirect_uri(deployment_id, provider), state=state, nonce=nonce,
        code_challenge=challenge, code_challenge_method='S256')
    if provider == 'google':
        params['hd'] = deployment.access_policy['google_domain']
    return config['authorize'] + '?' + urlencode(params)


def exchange_code(config, code, redirect, state):
    body = urlencode(dict(code=code, client_id=config['client'], client_secret=config['secret'],
        redirect_uri=redirect, grant_type='authorization_code', code_verifier=digest(state, 'pkce'))).encode()
    try:
        with urlopen(Request(config['token'], data=body, headers={'Content-Type': 'application/x-www-form-urlencoded'}), timeout=10) as response:
            payload = response.read(65537)
        if len(payload) > 65536:
            raise AccessDenied()
        return json.loads(payload)['id_token']
    except Exception:
        raise AccessDenied() from None


def validate_id_token(config, raw):
    try:
        # Pinned JWKS URL, never token-controlled jku/x5u. Only RS256 accepted.
        client = jwt.PyJWKClient(config['jwks'], timeout=5)
        key = client.get_signing_key_from_jwt(raw).key
        return jwt.decode(raw, key, algorithms=['RS256'], audience=config['client'], issuer=config['issuer'],
            options={'verify_signature': True, 'verify_exp': True, 'verify_iat': True,
                'verify_nbf': True, 'verify_iss': True, 'verify_aud': True,
                'enforce_minimum_key_length': True,
                'require': ['exp', 'iat', 'iss', 'aud', 'sub', 'nonce']})
    except Exception:
        raise AccessDenied() from None


def prepare_oidc(db, *, deployment_id, provider, state, browser):
    """Consume state in a short transaction; caller commits before provider I/O."""
    deployment = lock_deployment(db, deployment_id, joining=True)
    config = _provider(deployment, provider)
    proof = consume_proof(db, deployment_id=deployment_id, raw=state, browser=browser, kind=provider)
    return {'deployment_id': deployment_id, 'provider': provider, 'proof_id': proof.id,
        'nonce_digest': proof.nonce_digest, 'config': config, 'policy': dict(deployment.access_policy)}


def verify_oidc(prepared, *, state, code):
    """No session or authority lock exists while provider work is running."""
    config = prepared['config']
    claims = validate_id_token(config, exchange_code(config, code,
        redirect_uri(prepared['deployment_id'], prepared['provider']), state))
    if (not isinstance(claims.get('sub'), str) or not claims['sub']
        or not isinstance(claims.get('nonce'), str)
        or not hmac.compare_digest(prepared['nonce_digest'], digest(claims['nonce'], 'nonce'))
        or (claims.get('azp') and claims['azp'] != config['client'])
        or (isinstance(claims.get('aud'), list) and len(claims['aud']) > 1 and claims.get('azp') != config['client'])):
        raise AccessDenied()
    return claims


def finish_oidc(db, *, prepared, claims, browser):
    deployment_id, provider = prepared['deployment_id'], prepared['provider']
    deployment = lock_deployment(db, deployment_id, joining=True)
    proof = db.query(AccessProof).filter(AccessProof.id == prepared['proof_id']).populate_existing().with_for_update().first()
    if (proof is None or proof.kind != provider or not proof.consumed_at or proof.expires_at <= now_utc()
        or proof.deployment_id != deployment_id or deployment.access_policy != prepared['policy']
        or not hmac.compare_digest(proof.browser_digest, digest(browser, 'browser'))):
        raise AccessDenied()
    proof.kind = 'verified'  # The final grant also has one transactional winner.
    if provider == 'google':
        if claims.get('iss') not in GOOGLE_ISSUERS:
            raise AccessDenied()
        email = normalize_email(claims.get('email'))
        # Both documented issuer spellings identify the same Google subject.
        subject = hashlib.sha256((GOOGLE_ISSUERS[0] + ':' + claims['sub']).encode()).hexdigest()
        if (claims.get('email_verified') is not True
            or claims.get('hd') != deployment.access_policy['google_domain']
            or email.rsplit('@', 1)[1] != deployment.access_policy['google_domain']):
            raise AccessDenied()
        entitlement = grant_verified(db, deployment_id, email_digest(email), provider=provider, subject_digest=subject)
        return {'handoff': issue_handoff(db, entitlement.id)}
    if claims.get('tid', '').lower() != deployment.access_policy['microsoft_tenant'].lower():
        raise AccessDenied()
    # openid guarantees the validated stable sub; oid needs an unused profile
    # scope. Email is optional even with the email scope, so use mailbox proof.
    if not claims.get('email'):
        return {'email_required': True}
    email = normalize_email(claims['email'])
    subject = hashlib.sha256((claims['iss'] + ':' + claims['sub']).encode()).hexdigest()
    identity = db.query(AccessIdentity).filter(AccessIdentity.identity_digest == email_digest(email), AccessIdentity.provider == 'email').first()
    if identity and (identity.verification_metadata or {}).get(provider) == subject:
        entitlement = grant_verified(db, deployment_id, identity.identity_digest, provider=provider, subject_digest=subject)
        return {'handoff': issue_handoff(db, entitlement.id)}
    raw = begin_email(db, deployment_id=deployment_id, email=email, browser=browser, subject='microsoft:' + subject)
    return {'email': email, 'challenge': raw}

"""Opaque, purpose/deployment-scoped report grants. No legacy signed-plan access."""
import hashlib
import hmac
import re
import secrets
from datetime import timedelta

from app.access import AccessDenied, environment, require_access
from app.config import settings
from app.db import AIPlan, ReportAccessGrant
from app.privacy import utcnow

REPORT_TTL = timedelta(days=7)
PURPOSES = {'completion_report','pulse_report'}


def _digest(raw, purpose):
    context = f'{environment()}:{settings.DEPLOYMENT_ID}:{settings.ACCESS_KEY_VERSION}:{purpose}:{raw}'
    return hmac.new(settings.REPORT_TOKEN_SECRET.encode(), context.encode(), hashlib.sha256).hexdigest()


def issue_report_grant(db, user_id, plan_id, purpose):
    if purpose not in PURPOSES:
        raise AccessDenied()
    enrollment = require_access(db, user_id)
    if db.query(AIPlan.id).filter_by(id=plan_id,user_id=user_id).first() is None:
        raise AccessDenied()
    raw = secrets.token_urlsafe(32)
    now = utcnow()
    db.add(ReportAccessGrant(user_id=user_id,plan_id=plan_id,deployment_id=enrollment.deployment_id,
        purpose=purpose, token_digest=_digest(raw,purpose),token_version=settings.ACCESS_KEY_VERSION,
        issued_at=now,expires_at=now+REPORT_TTL))
    db.flush()
    return raw


def resolve_report_grant(db, raw, purpose):
    if purpose not in PURPOSES or not isinstance(raw,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',raw):
        raise AccessDenied()
    now = utcnow()
    grant = db.query(ReportAccessGrant).filter_by(token_digest=_digest(raw,purpose),purpose=purpose,
        token_version=settings.ACCESS_KEY_VERSION).first()
    if grant is None or grant.revoked_at is not None or grant.issued_at > now or grant.expires_at <= now:
        raise AccessDenied()
    enrollment = require_access(db,grant.user_id)
    if enrollment.deployment_id != grant.deployment_id:
        raise AccessDenied()
    return grant.plan_id


def revoke_report_grant(db, grant_id):
    grant = db.get(ReportAccessGrant,grant_id)
    if grant is not None and grant.revoked_at is None:
        grant.revoked_at = utcnow(); grant.revocation_reason='manual'


# Import compatibility for old integrations; deliberately no usable authority.
def make_report_token(plan_id, secret):
    raise RuntimeError('Use issue_report_grant inside the owned transaction')


def verify_report_token(token, secret):
    return None

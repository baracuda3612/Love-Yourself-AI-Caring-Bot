"""Read-only startup isolation check; binding is an explicit provisioning act."""
import hashlib
import json
import redis
from urllib.parse import urlsplit

from app.access import AccessDenied, environment
from app.config import settings
from app.db import (AccessAdminOperation, AccessEntitlement, AccessIdentity,
    AccessProof, Deployment, DeploymentEnrollment, DeploymentInvitation,
    DeploymentRosterEntry, DeploymentRosterVersion, RuntimeAccessBinding, User)


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _resource_url(value, *, instance=False):
    parsed = urlsplit(value)
    return f'{parsed.hostname}:{parsed.port or "default"}' + ('' if instance else parsed.path)


def resource_fingerprints():
    return {name: _hash(value) for name, value in {
        'database': _resource_url(settings.DATABASE_URL),
        'redis': _resource_url(settings.REDIS_URL, instance=True),
        'telegram_bot': settings.BOT_TOKEN.split(':')[0],
        'telegram_secret': settings.BOT_TOKEN,
        'openai_key': settings.OPENAI_API_KEY,
        'openai_project': settings.OPENAI_PROJECT_ID,
        'access_key': settings.ACCESS_TOKEN_SECRET,
        'operator_key': settings.ACCESS_OPERATOR_SECRET,
        'report_key': settings.REPORT_TOKEN_SECRET,
        'url': settings.APP_BASE_URL,
    }.items()}


def inventory_digest():
    actual = resource_fingerprints()
    if environment() == 'testnet' and any((settings.GOOGLE_CLIENT_SECRET, settings.MICROSOFT_CLIENT_SECRET,
        settings.SMTP_PASSWORD)):
        # Founder-only testnet has no production identity-provider credentials.
        raise AccessDenied()
    try:
        inventory = json.loads(settings.ACCESS_ISOLATION_INVENTORY)
        profile = inventory[environment()]
        if profile['resources'] != actual or profile['runtime_id'] != settings.DEPLOYMENT_ID:
            raise AccessDenied()
        # IDs for provider-managed scopes are release evidence, not inferred
        # from opaque credentials. A different key alone cannot prove a project.
        external = ('openai_project', 'backup_scope', 'alert_route', 'aggregate_sink')
        if any(not isinstance(profile.get(k), str) or not profile[k] for k in external):
            raise AccessDenied()
        if settings.OPENAI_PROJECT_ID and profile['openai_project'] != settings.OPENAI_PROJECT_ID:
            raise AccessDenied()
        other = inventory.get('testnet' if environment() == 'production' else 'production')
        if other is None:
            if environment() == 'production':
                raise AccessDenied()
        else:
            if (other['runtime_id'] == settings.DEPLOYMENT_ID
                or any(other['resources'].get(k) == v for k, v in actual.items())
                or any(profile[k] == other.get(k) for k in external)):
                raise AccessDenied()
        # Bind stable infrastructure, not rotatable secret fingerprints.
        stable = {k: actual[k] for k in ('database', 'redis', 'telegram_bot', 'url')}
        stable.update({k: profile[k] for k in external})
        return _hash(json.dumps(stable, sort_keys=True))
    except (ValueError, KeyError, TypeError, AttributeError):
        raise AccessDenied() from None


def bind_environment(db):
    """Explicitly bind only a fresh contour with no access authority."""
    signature = inventory_digest()
    authority_tables = (User, Deployment, AccessIdentity, DeploymentRosterVersion,
        DeploymentRosterEntry, AccessEntitlement, DeploymentInvitation,
        DeploymentEnrollment, AccessProof, AccessAdminOperation)
    if (db.get(RuntimeAccessBinding, 1)
        or any(db.query(model.id).first() for model in authority_tables)):
        raise AccessDenied()
    expected = f'{environment()}:{settings.DEPLOYMENT_ID}'
    try:
        client = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=2, socket_timeout=2, decode_responses=True)
        client.set('ly:access:runtime-binding', expected, nx=True)
        if client.get('ly:access:runtime-binding') != expected:
            raise AccessDenied()
    except Exception:
        raise AccessDenied() from None
    db.add(RuntimeAccessBinding(singleton=1, environment=environment(),
        runtime_id=settings.DEPLOYMENT_ID, resource_digest=signature))
    db.flush()


def audit_environment(db):
    signature = inventory_digest()
    binding = db.get(RuntimeAccessBinding, 1)
    if (binding is None or binding.environment != environment()
        or binding.runtime_id != settings.DEPLOYMENT_ID or binding.resource_digest != signature
        or db.query(Deployment.id).filter(Deployment.environment != environment()).first()):
        raise AccessDenied()
    try:
        client = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=2, socket_timeout=2, decode_responses=True)
        if client.get('ly:access:runtime-binding') != f'{environment()}:{settings.DEPLOYMENT_ID}':
            raise AccessDenied()
    except Exception:
        raise AccessDenied() from None

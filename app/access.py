"""Deployment access authority. Callers own commit/rollback; no behavior logging.

Lock order: existing personal user -> deployment -> subordinate access rows.
Roster/proof operations never lock users. Telegram creation uses an advisory
actor lock before the same order. Revocation and issuance serialize on deployment.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, text, select, exists, or_
from sqlalchemy.dialects.postgresql import insert

from app.config import settings
from app.db import (AccessAdminOperation, AccessEntitlement, AccessIdentity, AccessProof, Deployment,
    DeploymentEnrollment, DeploymentInvitation, DeploymentRosterEntry,
    DeploymentRosterVersion, User, UserProfile)

TOKEN_TTL = timedelta(minutes=10)
PROOF_TTL = timedelta(minutes=10)
PREVIEW_TTL = timedelta(minutes=30)
NEUTRAL_DENIAL = "access_unavailable"


class AccessDenied(ValueError):
    def __init__(self):
        super().__init__(NEUTRAL_DENIAL)


def now_utc():
    return datetime.now(timezone.utc)


def environment():
    return "production" if settings.ENVIRONMENT == "prod" else "testnet"


def digest(value: str, purpose: str) -> str:
    return hmac.new(settings.ACCESS_TOKEN_SECRET.encode(),
        f"{environment()}:{settings.DEPLOYMENT_ID}:{settings.ACCESS_KEY_VERSION}:{purpose}:{value}".encode(), hashlib.sha256).hexdigest()


def normalize_email(value):
    if not isinstance(value, str) or len(value) > 254:
        raise AccessDenied()
    value = value.strip().casefold()
    if not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}", value):
        raise AccessDenied()
    return value


def email_digest(email):
    # Restricted lookup, independent of rotatable invitation keys.
    return hashlib.sha256(normalize_email(email).encode()).hexdigest()


def lock_deployment(db, deployment_id, *, joining=False, delivery=False, now=None, lock=True):
    query = db.query(Deployment).filter(Deployment.id == deployment_id).populate_existing()
    deployment = (query.with_for_update() if lock else query).first()
    now = now or now_utc()
    if (deployment is None or deployment.environment != environment()
        or (deployment.starts_at and deployment.starts_at > now)
        or (deployment.ends_at and deployment.ends_at <= now)
        or (joining and not deployment.enrollment_open)
        or (delivery and not deployment.delivery_enabled)):
        raise AccessDenied()
    return deployment


def require_access(db, user_id, *, delivery=False, now=None):
    # The lifecycle caller holds the user lock. Standalone callers must also
    # lock it, so enrollment replacement cannot change the binding mid-check.
    user = db.query(User).filter(User.id == user_id).populate_existing().with_for_update().first()
    if user is None or user.is_active is not True:
        raise AccessDenied()
    enrollment = db.query(DeploymentEnrollment).filter(
        DeploymentEnrollment.user_id == user_id, DeploymentEnrollment.ended_at.is_(None)).first()
    if enrollment is None:
        raise AccessDenied()
    deployment = lock_deployment(db, enrollment.deployment_id, delivery=delivery, now=now)
    now = now or now_utc()
    entitlement = db.query(AccessEntitlement).filter(AccessEntitlement.id == enrollment.entitlement_id).populate_existing().first()
    if (entitlement is None or entitlement.deployment_id != deployment.id
        or entitlement.granted_at > now or (entitlement.revoked_at and entitlement.revoked_at <= now)):
        raise AccessDenied()
    if enrollment.enrolled_at > now:
        raise AccessDenied()
    return enrollment


def outbound_claim(user_id, *, delivery=False):
    """One fresh access snapshot before non-durable conversational/pulse I/O.

    This read makes no related mutation, so one statement supplies a coherent
    authority snapshot without serializing every company reply on its row.
    Durable exercise/completion claims and lifecycle mutations keep their locks.
    """
    from app.db import SessionLocal
    with SessionLocal() as db:
        return db.execute(select(func.now()).select_from(User).where(
            User.id == user_id, User.is_active.is_(True),
            access_predicate(User.id, delivery=delivery),
        )).scalar_one_or_none()


def may_access(db, user_id, *, delivery=False):
    try:
        require_access(db, user_id, delivery=delivery)
        return True
    except AccessDenied:
        return False


def access_predicate(user_id, *, delivery=False):
    """Current SQL authority; durable send/mutation claims additionally lock."""
    at = func.now()
    statement = select(DeploymentEnrollment.id).join(AccessEntitlement,
        AccessEntitlement.id == DeploymentEnrollment.entitlement_id).join(Deployment,
        Deployment.id == DeploymentEnrollment.deployment_id).where(
        DeploymentEnrollment.user_id == user_id, DeploymentEnrollment.ended_at.is_(None),
        DeploymentEnrollment.enrolled_at <= at, AccessEntitlement.granted_at <= at,
        or_(AccessEntitlement.revoked_at.is_(None), AccessEntitlement.revoked_at > at),
        Deployment.environment == environment(),
        or_(Deployment.starts_at.is_(None), Deployment.starts_at <= at),
        or_(Deployment.ends_at.is_(None), Deployment.ends_at > at))
    if delivery:
        statement = statement.where(Deployment.delivery_enabled.is_(True))
    return exists(statement)


def require_planning_timezone(user):
    try:
        if not user.timezone:
            raise AccessDenied()
        ZoneInfo(user.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise AccessDenied() from None


def _roster(db, deployment_id):
    version = db.query(DeploymentRosterVersion).filter(
        DeploymentRosterVersion.deployment_id == deployment_id,
        DeploymentRosterVersion.is_current.is_(True)).first()
    if version is None:
        return None, set()
    ids = {row.access_identity_id for row in db.query(DeploymentRosterEntry).filter(
        DeploymentRosterEntry.roster_version_id == version.id,
        DeploymentRosterEntry.delta_action == 'add').all()}
    return version, ids


@dataclass(frozen=True)
class RosterPreview:
    version_id: int
    counts: dict
    confirmation: str


def preview_roster(db, *, deployment_id, mode, rows, expected_total, source_ref, operator):
    deployment = lock_deployment(db, deployment_id, lock=False)
    policy = dict(deployment.access_policy)
    if (mode not in {"full_snapshot", "delta"} or not isinstance(rows, list)
        or not 1 <= len(rows) <= 10000 or not isinstance(expected_total, int)
        or isinstance(expected_total, bool) or expected_total < 0
        or not isinstance(source_ref, str) or not 1 <= len(source_ref) <= 160):
        raise AccessDenied()
    domains = deployment.access_policy.get('allowed_domains', [])
    if deployment.environment == 'production' and (not isinstance(domains, list) or not domains
        or any(not isinstance(domain, str) or domain != domain.casefold() or '@' in domain for domain in domains)):
        raise AccessDenied()
    current, previous = _roster(db, deployment_id)
    changes = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'email', 'action'}:
            raise AccessDenied()
        email = normalize_email(row['email'])
        action = row['action']
        if (email in changes or action not in {'add', 'revoke'}
            or (mode == 'full_snapshot' and action != 'add')
            or (domains and email.rsplit('@', 1)[1] not in domains)):
            raise AccessDenied()
        changes[email] = action
    identities = [{'identity_digest': email_digest(email), 'normalized_email': email,
        'provider': 'email', 'verification_metadata': {}} for email in changes]
    db.execute(insert(AccessIdentity).on_conflict_do_nothing(), identities)
    identity_rows = db.query(AccessIdentity).filter(AccessIdentity.normalized_email.in_(changes), AccessIdentity.provider == 'email').all()
    if len(identity_rows) != len(changes):
        raise AccessDenied()
    additions, removals, explicit_actions = set(), set(), {}
    for identity in identity_rows:
        action = changes[identity.normalized_email]
        explicit_actions[identity.id] = action
        (additions if action == 'add' else removals).add(identity.id)
    effective = additions if mode == 'full_snapshot' else (previous | additions) - removals
    if len(effective) != expected_total or (mode == 'full_snapshot' and not effective):
        raise AccessDenied()
    counts = {'add': len(effective - previous), 'keep': len(effective & previous), 'revoke': len(previous - effective), 'total': len(effective)}
    deployment = lock_deployment(db, deployment_id)
    accepted = db.query(DeploymentRosterVersion).filter(
        DeploymentRosterVersion.deployment_id == deployment_id,
        DeploymentRosterVersion.is_current.is_(True)).first()
    if (accepted.version if accepted else 0) != (current.version if current else 0) or deployment.access_policy != policy:
        raise AccessDenied()
    next_version = (db.query(func.max(DeploymentRosterVersion.version)).filter(
        DeploymentRosterVersion.deployment_id == deployment_id).scalar() or 0) + 1
    confirmation = secrets.token_urlsafe(32)
    at = now_utc()
    version = DeploymentRosterVersion(deployment_id=deployment_id, version=next_version,
        import_mode=mode, source_ref=source_ref, eligible_count=len(effective), invalid_count=0,
        validated_at=at, base_version=current.version if current else 0, preview_counts=counts,
        confirmation_digest=digest(confirmation, 'roster-confirm'), preview_expires_at=at + PREVIEW_TTL,
        operator_digest=digest(operator, 'operator'))
    db.add(version); db.flush()
    # Each version materializes its effective access set, including delta
    # carry-forward. The source mode and explicit removals remain auditable.
    entries = [dict(roster_version_id=version.id, access_identity_id=identity_id,
        entry_key=str(identity_id), delta_action='add' if identity_id in effective else 'remove',
        validation_metadata={'input_action': explicit_actions.get(identity_id, 'carried')})
        for identity_id in sorted(effective | (previous - effective))]
    db.execute(insert(DeploymentRosterEntry), entries)
    db.flush()
    return RosterPreview(version.id, counts, confirmation)


def apply_roster(db, *, deployment_id, version_id, confirmation, counts, operator):
    lock_deployment(db, deployment_id)
    version = db.query(DeploymentRosterVersion).filter(DeploymentRosterVersion.id == version_id,
        DeploymentRosterVersion.deployment_id == deployment_id).populate_existing().with_for_update().first()
    at = now_utc()
    if (version is None or version.applied_at or not version.confirmation_digest
        or version.preview_expires_at <= at or counts != version.preview_counts
        or not isinstance(counts, dict) or any(type(value) is not int for value in counts.values())
        or not hmac.compare_digest(version.confirmation_digest, digest(confirmation, 'roster-confirm'))
        or not hmac.compare_digest(version.operator_digest, digest(operator, 'operator'))):
        raise AccessDenied()
    current, previous = _roster(db, deployment_id)
    if (current.version if current else 0) != version.base_version:
        raise AccessDenied()
    effective = {r.access_identity_id for r in db.query(DeploymentRosterEntry).filter(
        DeploymentRosterEntry.roster_version_id == version.id, DeploymentRosterEntry.delta_action == 'add')}
    open_entitlements = db.query(AccessEntitlement).filter(AccessEntitlement.deployment_id == deployment_id,
        AccessEntitlement.revoked_at.is_(None))
    revoked_ids = [row.id for row in open_entitlements.filter(~AccessEntitlement.access_identity_id.in_(effective)).all()]
    if revoked_ids:
        open_entitlements.filter(AccessEntitlement.id.in_(revoked_ids)).update({'revoked_at': at}, synchronize_session='fetch')
        db.query(DeploymentInvitation).filter(DeploymentInvitation.entitlement_id.in_(revoked_ids),
            DeploymentInvitation.redeemed_at.is_(None), DeploymentInvitation.revoked_at.is_(None)).update({'revoked_at': at}, synchronize_session='fetch')
    open_entitlements.filter(AccessEntitlement.access_identity_id.in_(effective)).update(
        {'confirming_roster_version_id': version.id}, synchronize_session='fetch')
    if current:
        current.is_current = False
        db.flush()
    version.applied_at = at; version.is_current = True
    version.confirmation_digest = None
    db.flush()
    return version


def grant_verified(db, deployment_id, identity_key, *, provider='email', subject_digest=None):
    deployment = lock_deployment(db, deployment_id, joining=True)
    if deployment.environment != 'production':
        raise AccessDenied()
    version, members = _roster(db, deployment_id)
    identity = db.query(AccessIdentity).filter(AccessIdentity.identity_digest == identity_key,
        AccessIdentity.provider == 'email').with_for_update().first()
    if identity is None or identity.id not in members:
        raise AccessDenied()
    metadata = dict(identity.verification_metadata or {})
    if subject_digest:
        # Stable subject is purpose-limited and never enters user telemetry.
        prior = metadata.get(provider)
        if prior and prior != subject_digest:
            raise AccessDenied()
        metadata[provider] = subject_digest
    identity.verification_metadata = metadata
    identity.verified_at = now_utc()
    entitlement = db.query(AccessEntitlement).filter(AccessEntitlement.deployment_id == deployment_id,
        AccessEntitlement.access_identity_id == identity.id, AccessEntitlement.revoked_at.is_(None)).first()
    if entitlement is None:
        prior = db.query(AccessEntitlement).filter(AccessEntitlement.deployment_id == deployment_id,
            AccessEntitlement.access_identity_id == identity.id).order_by(AccessEntitlement.granted_at.desc(), AccessEntitlement.id.desc()).first()
        if prior is not None:
            restored = db.query(DeploymentRosterEntry.id).join(DeploymentRosterVersion,
                DeploymentRosterVersion.id == DeploymentRosterEntry.roster_version_id).filter(
                DeploymentRosterVersion.deployment_id == deployment_id,
                DeploymentRosterVersion.applied_at > prior.revoked_at,
                DeploymentRosterEntry.access_identity_id == identity.id,
                DeploymentRosterEntry.delta_action == 'add',
                DeploymentRosterEntry.validation_metadata['input_action'].astext == 'add').first()
            # Neither a fresh proof nor an unrelated delta can undo a manual
            # revoke. Restoration requires a later explicitly accepted add.
            if restored is None:
                raise AccessDenied()
        entitlement = AccessEntitlement(deployment_id=deployment_id, access_identity_id=identity.id,
            granted_at=now_utc(), source='verified_identity', granting_roster_version_id=version.id,
            confirming_roster_version_id=version.id)
        db.add(entitlement); db.flush()
    return entitlement


def issue_handoff(db, entitlement_id):
    hint = db.get(AccessEntitlement, entitlement_id)
    if hint is None:
        raise AccessDenied()
    lock_deployment(db, hint.deployment_id, joining=True)
    entitlement = db.query(AccessEntitlement).filter(AccessEntitlement.id == entitlement_id).populate_existing().with_for_update().first()
    at = now_utc()
    if entitlement.revoked_at is not None or entitlement.granted_at > at:
        raise AccessDenied()
    # New issuance invalidates an abandoned prior link; never retrieve raw.
    db.query(DeploymentInvitation).filter(DeploymentInvitation.entitlement_id == entitlement_id,
        DeploymentInvitation.redeemed_at.is_(None), DeploymentInvitation.revoked_at.is_(None)).update({'revoked_at': at})
    raw = f"ly{settings.ACCESS_KEY_VERSION}_" + secrets.token_urlsafe(32)
    db.add(DeploymentInvitation(entitlement_id=entitlement_id, token_digest=digest(raw, 'handoff'),
        token_version=settings.ACCESS_KEY_VERSION, source_operation_id='handoff:' + secrets.token_hex(24),
        issued_at=at, expires_at=at + TOKEN_TTL))
    db.flush()
    return raw


def redeem_handoff(db, raw, tg_user):
    if not isinstance(raw, str) or not re.fullmatch(r'ly[0-9]{1,7}_[A-Za-z0-9_-]{43}', raw):
        raise AccessDenied()
    if type(tg_user.id) is not int or tg_user.id <= 0:
        raise AccessDenied()
    db.execute(text('SELECT pg_advisory_xact_lock(:actor)'), {'actor': tg_user.id})
    user = db.query(User).filter(User.tg_id == tg_user.id).with_for_update().first()
    hint = db.query(DeploymentInvitation).filter(DeploymentInvitation.token_digest == digest(raw, 'handoff')).first()
    if hint is None:
        raise AccessDenied()
    entitlement_hint = db.get(AccessEntitlement, hint.entitlement_id)
    deployment = lock_deployment(db, entitlement_hint.deployment_id, joining=True)
    entitlement = db.query(AccessEntitlement).filter(AccessEntitlement.id == hint.entitlement_id).populate_existing().with_for_update().first()
    token = db.query(DeploymentInvitation).filter(DeploymentInvitation.id == hint.id).populate_existing().with_for_update().first()
    at = now_utc()
    if (token.token_version != settings.ACCESS_KEY_VERSION or token.redeemed_at or token.revoked_at
        or token.issued_at > at or token.expires_at <= at or entitlement.granted_at > at
        or entitlement.revoked_at is not None):
        raise AccessDenied()
    bound = db.query(DeploymentEnrollment).filter(DeploymentEnrollment.entitlement_id == entitlement.id,
        DeploymentEnrollment.ended_at.is_(None)).first()
    current = None if user is None else db.query(DeploymentEnrollment).filter(
        DeploymentEnrollment.user_id == user.id, DeploymentEnrollment.ended_at.is_(None)).first()
    if bound and (user is None or bound.user_id != user.id):
        raise AccessDenied()
    if current and current.entitlement_id != entitlement.id:
        old = db.get(AccessEntitlement, current.entitlement_id)
        if (old.deployment_id != entitlement.deployment_id or old.access_identity_id != entitlement.access_identity_id
            or old.revoked_at is None or old.revoked_at > at):
            raise AccessDenied()
        current.ended_at = max(at, current.enrolled_at + timedelta(microseconds=1))
        current.ended_reason = 'entitlement_regrant'
        db.flush()
        current = None
    created = user is None
    if created:
        user = User(tg_id=tg_user.id, username=tg_user.username, first_name=tg_user.first_name)
        db.add(user); db.flush()
    if db.query(UserProfile).filter(UserProfile.user_id == user.id).first() is None:
        db.add(UserProfile(user_id=user.id))
    if user.is_active is False:
        raise AccessDenied()
    if current is None:
        current = DeploymentEnrollment(user_id=user.id, deployment_id=deployment.id,
            entitlement_id=entitlement.id, enrolled_at=at,
            attribution_source='testnet' if deployment.environment == 'testnet' else 'verified_identity')
        db.add(current); db.flush()
    from app.lifecycle import ensure_onboarding_progress
    progress = ensure_onboarding_progress(db, user.id)
    progress.deployment_enrollment_id = current.id
    token.redeemed_at = at
    db.flush()
    return user, created


def founder_grant(db, *, deployment_id, operator, label):
    deployment = lock_deployment(db, deployment_id, joining=True)
    if deployment.environment != 'testnet' or not operator or not isinstance(label, str) or not 1 <= len(label) <= 96:
        raise AccessDenied()
    # Synthetic identity is restricted access data; no test address/production
    # identity reuse, and no behavior/telemetry difference after redemption.
    key = hashlib.sha256(f'testnet:{deployment_id}:{label}'.encode()).hexdigest()
    db.execute(insert(AccessIdentity).values(identity_digest=key, provider='founder',
        verified_at=now_utc(), verification_metadata={}).on_conflict_do_nothing())
    identity = db.query(AccessIdentity).filter(AccessIdentity.identity_digest == key, AccessIdentity.provider == 'founder').first()
    entitlement = db.query(AccessEntitlement).filter(AccessEntitlement.deployment_id == deployment_id,
        AccessEntitlement.access_identity_id == identity.id, AccessEntitlement.revoked_at.is_(None)).first()
    if entitlement is None:
        entitlement = AccessEntitlement(deployment_id=deployment_id, access_identity_id=identity.id,
            granted_at=now_utc(), source='founder')
        db.add(entitlement); db.flush()
        audit_admin(db, deployment_id, operator, 'founder_grant', entitlement.id)
    return entitlement


def revoke_entitlement(db, *, entitlement_id, operator='internal'):
    hint = db.get(AccessEntitlement, entitlement_id)
    if hint is None:
        raise AccessDenied()
    lock_deployment(db, hint.deployment_id)
    entitlement = db.query(AccessEntitlement).filter(AccessEntitlement.id == entitlement_id).populate_existing().with_for_update().first()
    at = now_utc()
    entitlement.revoked_at = entitlement.revoked_at or at
    audit_admin(db, entitlement.deployment_id, operator, 'revoke', entitlement_id)
    db.query(DeploymentInvitation).filter(DeploymentInvitation.entitlement_id == entitlement_id,
        DeploymentInvitation.redeemed_at.is_(None), DeploymentInvitation.revoked_at.is_(None)).update({'revoked_at': at})
    db.flush()


def audit_admin(db, deployment_id, operator, action, target_id=None):
    db.add(AccessAdminOperation(deployment_id=deployment_id, operator_digest=digest(operator, 'operator'),
        action=action, target_id=target_id, occurred_at=now_utc()))


def end_enrollment_for_replacement(db, *, user_id, expected_enrollment_id, handoff,
                                  operator, replacement_mode='same_identity'):
    """Audited manual support after new proof, never self-service recovery.

    A freshly issued unspent handoff proves the target identity was verified.
    Closing membership preserves every personal/history row and attribution.
    """
    if (type(user_id) is not int or type(expected_enrollment_id) is not int
        or replacement_mode not in {'same_identity', 'identity_change'}):
        raise AccessDenied()
    user = db.query(User).filter(User.id == user_id).with_for_update().first()
    if user is None:
        raise AccessDenied()
    token = db.query(DeploymentInvitation).filter(DeploymentInvitation.token_digest == digest(handoff, 'handoff')).first()
    if token is None:
        raise AccessDenied()
    ent = db.get(AccessEntitlement, token.entitlement_id)
    current = db.query(DeploymentEnrollment).filter(DeploymentEnrollment.user_id == user_id,
        DeploymentEnrollment.ended_at.is_(None)).first()
    if current is None or current.id != expected_enrollment_id:
        raise AccessDenied()
    # Both old/new deployments serialize replacement with their authority.
    for deployment_id in sorted({current.deployment_id, ent.deployment_id}):
        # The old commercial period may have ended; only environment matters.
        old = db.query(Deployment).filter(Deployment.id == deployment_id).populate_existing().with_for_update().one()
        if old.environment != environment():
            raise AccessDenied()
    lock_deployment(db, ent.deployment_id, joining=True)
    db.refresh(ent); db.refresh(token)
    at = now_utc()
    if (ent.revoked_at is not None or token.redeemed_at or token.revoked_at or token.expires_at <= at
        or token.token_version != settings.ACCESS_KEY_VERSION):
        raise AccessDenied()
    previous = db.get(AccessEntitlement, current.entitlement_id)
    if replacement_mode == 'same_identity' and (
        ent.deployment_id != current.deployment_id
        or ent.access_identity_id != previous.access_identity_id
    ):
        raise AccessDenied()
    current.ended_at = max(at, current.enrolled_at + timedelta(microseconds=1))
    # The immutable operator audit points to the old enrollment; its ending
    # reason records the explicitly chosen operation and target entitlement.
    current.ended_reason = f'operator_{replacement_mode}:{ent.id}'
    action = 'replace_identity' if replacement_mode == 'identity_change' else 'end_enrollment'
    audit_admin(db, current.deployment_id, operator, action, current.id)
    db.flush()


def set_deployment_controls(db, *, deployment_id, enrollment_open, delivery_enabled, operator):
    deployment = lock_deployment(db, deployment_id)
    if type(enrollment_open) is not bool or type(delivery_enabled) is not bool:
        raise AccessDenied()
    deployment.enrollment_open = enrollment_open
    deployment.delivery_enabled = delivery_enabled
    audit_admin(db, deployment_id, operator, 'controls')
    db.flush()
    # A repeated "enabled" operator request can re-arm a lost post-commit wake.
    return delivery_enabled

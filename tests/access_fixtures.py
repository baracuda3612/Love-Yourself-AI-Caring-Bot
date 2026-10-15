"""Explicit valid access fixtures for earlier product-invariant rehearsals."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from app.db import (AccessEntitlement, AccessIdentity, Deployment, DeploymentEnrollment,
    Organization, PrivacyNoticeVersion)


def deployment(db, *, environment='testnet', mode='single', zone='UTC'):
    key = uuid4().hex
    notice = PrivacyNoticeVersion(version=key, published_at=datetime.now(timezone.utc)-timedelta(days=3650),
        content_digest=key, content_location='test-only', is_current=False)
    organization = Organization(organization_key=key, display_name='disposable fixture', commercial_metadata={})
    db.add_all([notice, organization]); db.flush()
    row = Deployment(organization_id=organization.id, deployment_key=key, environment=environment,
        enrollment_open=True, delivery_enabled=True, timezone_mode=mode, default_timezone=zone,
        notice_version_id=notice.id, access_policy={'allowed_domains':['client.org']} if environment=='production' else {})
    db.add(row); db.flush()
    return row


def enroll(db, user, *, target=None):
    target = target or deployment(db)
    at = datetime.now(timezone.utc)-timedelta(days=3650)
    identity = AccessIdentity(identity_digest=uuid4().hex, provider='founder', verified_at=at, verification_metadata={})
    db.add(identity); db.flush()
    entitlement = AccessEntitlement(deployment_id=target.id, access_identity_id=identity.id,
        granted_at=at, source='founder')
    db.add(entitlement); db.flush()
    row = DeploymentEnrollment(user_id=user.id, deployment_id=target.id, entitlement_id=entitlement.id,
        enrolled_at=at, attribution_source='testnet')
    db.add(row); db.flush()
    return row

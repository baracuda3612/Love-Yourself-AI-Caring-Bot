"""Minimum privacy authority. Local operators own manual fulfillment, never Coach.

Lock order is user -> deployments -> request. Callers commit notice/request
operations; delete_account owns its explicit recovery transactions.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from uuid import UUID, uuid4

from sqlalchemy import or_, and_, select
from sqlalchemy.dialects.postgresql import insert

from app import db as models
from app.access import AccessDenied, environment, require_access

RETENTION = timedelta(days=90)
REQUEST_KINDS = {'access', 'export', 'correction', 'deletion'}
SUPPORT_CONTACT = 'hello@loveyourselfua.com'


class NoticeRequired(AccessDenied):
    pass


def utcnow():
    return datetime.now(timezone.utc)


def pinned_notice(db, user_id, *, acknowledged=False):
    enrollment = require_access(db, user_id)
    deployment = db.get(models.Deployment, enrollment.deployment_id)
    notice = db.get(models.PrivacyNoticeVersion, deployment.notice_version_id)
    if (notice is None or not notice.notice_text or notice.retired_at is not None
        or notice.published_at > utcnow()
        or sha256(notice.notice_text.encode()).hexdigest() != notice.content_digest):
        raise NoticeRequired()
    if acknowledged and db.query(models.NoticeAcknowledgement.id).filter_by(
        user_id=user_id, deployment_id=deployment.id, notice_version_id=notice.id).first() is None:
        raise NoticeRequired()
    return deployment, notice


def acknowledge_notice(db, user_id, notice_id, source_operation_id):
    deployment, notice = pinned_notice(db, user_id)
    if notice.id != notice_id:
        raise AccessDenied()
    # An actual callback naming the immutable shown version supplies evidence.
    db.execute(insert(models.NoticeAcknowledgement).values(
        user_id=user_id, deployment_id=deployment.id, notice_version_id=notice.id,
        acknowledged_at=utcnow(), source_operation_id=source_operation_id,
    ).on_conflict_do_nothing(index_elements=['user_id','deployment_id','notice_version_id']))


def register_notice(db, *, version, notice_text, location):
    """Operator installs only the founder/legal approved exact UTF-8 artifact."""
    if not notice_text or len(notice_text) > 3000 or not version.strip():
        raise ValueError('notice_must_fit_one_telegram_message')
    row = models.PrivacyNoticeVersion(version=version, notice_text=notice_text,
        content_digest=sha256(notice_text.encode()).hexdigest(), content_location=location,
        published_at=utcnow(), is_current=False)
    db.add(row); db.flush()
    return row


def receive_request(db, *, tg_id, kind, source_operation_id, case_id=None):
    """Only private Telegram transport callers may supply the authenticated tg_id.

    Expired entitlement never removes rights access. No user-supplied internal
    id, email or username can identify the subject of this operation.
    """
    if kind not in REQUEST_KINDS:
        raise ValueError('unknown_request_kind')
    user = db.query(models.User).filter_by(tg_id=tg_id).with_for_update().first()
    if user is None:
        raise AccessDenied()
    enrollment = (db.query(models.DeploymentEnrollment).join(models.Deployment)
        .filter(models.DeploymentEnrollment.user_id == user.id,
            models.Deployment.environment == environment())
        .order_by(models.DeploymentEnrollment.enrolled_at.desc()).first())
    if enrollment is None:
        raise AccessDenied()
    deployment = db.get(models.Deployment, enrollment.deployment_id)
    # A contact without an accountable operator cannot receive a real request.
    owner = deployment.support_contact_ref or SUPPORT_CONTACT
    if not owner or not owner.strip():
        raise AccessDenied()
    if case_id is not None:
        case = db.query(models.PrivacyRequest).filter_by(id=UUID(str(case_id))).with_for_update().first()
        if case is None or case.kind != kind or case.state != 'received' or case.user_id not in {None,user.id}:
            raise AccessDenied()
        if case.verified_at is None:
            case.user_id = user.id
            case.verified_at = utcnow()
            case.source_operation_id = source_operation_id
        return case
    existing = db.query(models.PrivacyRequest).filter_by(source_operation_id=source_operation_id).first()
    if existing:
        if existing.user_id != user.id or existing.kind != kind:
            raise AccessDenied()
        return existing
    pending = db.query(models.PrivacyRequest).filter_by(user_id=user.id, kind=kind).filter(
        models.PrivacyRequest.state != 'completed').first()
    if pending:
        return pending
    now = utcnow()
    row = models.PrivacyRequest(id=uuid4(), user_id=user.id, kind=kind, state='received',
        source_operation_id=source_operation_id, owner=owner, received_at=now,
        verified_at=now, due_at=now+timedelta(days=30), evidence={}, expires_at=now+RETENTION)
    db.add(row); db.flush()
    return row


def record_manual_request(db, *, kind, received_at=None):
    """Record an email intake before identity verification; no sensitive body copy."""
    if kind not in REQUEST_KINDS:
        raise ValueError('unknown_request_kind')
    at = received_at or utcnow()
    if at > utcnow() or at.tzinfo is None:
        raise ValueError('invalid_received_time')
    row = models.PrivacyRequest(id=uuid4(), user_id=None, kind=kind, state='received',
        source_operation_id=None, owner=SUPPORT_CONTACT, received_at=at,
        verified_at=None, due_at=at+timedelta(days=30), evidence={}, expires_at=at+RETENTION)
    db.add(row); db.flush()
    return row


def pending_request_page(db, *, after=None, limit=100):
    """Keyset paging keeps every pending/overdue case reachable without a scan."""
    limit = max(1, min(limit, 100))
    query = db.query(models.PrivacyRequest).filter(models.PrivacyRequest.state != 'completed')
    if after is not None:
        anchor = db.get(models.PrivacyRequest, UUID(str(after)))
        if anchor is None:
            raise ValueError('queue_cursor_unavailable')
        query = query.filter(or_(models.PrivacyRequest.due_at > anchor.due_at,
            and_(models.PrivacyRequest.due_at == anchor.due_at,
                models.PrivacyRequest.id > anchor.id)))
    rows = query.order_by(models.PrivacyRequest.due_at, models.PrivacyRequest.id).limit(limit + 1).all()
    return rows[:limit], str(rows[limit - 1].id) if len(rows) > limit else None


def owned_request(db, request_id, *, kind=None):
    row = db.get(models.PrivacyRequest, UUID(str(request_id)))
    if row is None or (kind and row.kind != kind) or row.verified_at is None:
        raise ValueError('request_unavailable')
    return row


def _rows(db, model, predicate):
    return [dict(row) for row in db.execute(select(model.__table__).where(predicate)).mappings()]


def export_request(db, request_id):
    """Restricted support export. Delivery to the verified subject is separate."""
    request = owned_request(db, request_id)
    if request.kind not in {'access','export'} or request.state != 'received' or not request.user_id:
        raise ValueError('request_unavailable')
    user_id = request.user_id
    user = db.query(models.User).filter_by(id=user_id).with_for_update().one()
    plan_ids = db.query(models.AIPlan.id).filter_by(user_id=user_id)
    day_ids = db.query(models.AIPlanDay.id).filter(models.AIPlanDay.plan_id.in_(plan_ids))
    now = utcnow()
    data = {'format':'love-yourself-personal-export-v1', 'generated_at':now,
        'retention_days':90}
    for model in (models.User, models.UserProfile, models.OnboardingProgress,
        models.AIPlan, models.PlanDraftRecord, models.UserEvent, models.FeedbackEvent,
        models.NoticeAcknowledgement, models.DeploymentEnrollment, models.TaskStats,
        models.FailureSignal, models.PlanInstance, models.AggregateRecord):
        column = model.id if model is models.User else model.user_id
        predicate = column == user.id
        if model is models.FeedbackEvent:
            retained_ids = select(models.ChatHistory.id).where(
                models.ChatHistory.user_id == user_id, models.ChatHistory.expires_at > now,
                models.ChatHistory.created_at > now-RETENTION)
            predicate = predicate & (models.FeedbackEvent.created_at > now-RETENTION) & (
                models.FeedbackEvent.coach_message_id.is_(None) | models.FeedbackEvent.coach_message_id.in_(retained_ids)) & (
                models.FeedbackEvent.source_message_id.is_(None) | models.FeedbackEvent.source_message_id.in_(retained_ids))
        rows = _rows(db, model, predicate)
        data[model.__tablename__] = rows
    data['chat_history'] = _rows(db, models.ChatHistory,
        (models.ChatHistory.user_id == user_id) & (models.ChatHistory.expires_at > now)
        & (models.ChatHistory.created_at > now-RETENTION))
    data['ai_plan_days'] = _rows(db, models.AIPlanDay, models.AIPlanDay.plan_id.in_(plan_ids))
    data['ai_plan_steps'] = _rows(db, models.AIPlanStep, models.AIPlanStep.day_id.in_(day_ids))
    step_ids = select(models.AIPlanStep.id).where(models.AIPlanStep.day_id.in_(day_ids))
    draft_ids = select(models.PlanDraftRecord.id).where(models.PlanDraftRecord.user_id == user_id)
    instance_ids = select(models.PlanInstance.id).where(models.PlanInstance.user_id == user_id)
    for model, predicate in (
        (models.ExerciseDelivery, models.ExerciseDelivery.plan_step_id.in_(step_ids)),
        (models.PlanCompletion, models.PlanCompletion.plan_id.in_(plan_ids)),
        (models.AIPlanVersion, models.AIPlanVersion.plan_id.in_(plan_ids)),
        (models.PlanDraftStep, models.PlanDraftStep.draft_id.in_(draft_ids)),
        (models.PlanExecutionWindow, models.PlanExecutionWindow.instance_id.in_(instance_ids)),
        (models.PlanLifecycleOperation, models.PlanLifecycleOperation.user_id == user_id),
    ):
        data[model.__tablename__] = _rows(db, model, predicate)
    enrollment_ids = [r['entitlement_id'] for r in data['deployment_enrollments']]
    entitlements = _rows(db, models.AccessEntitlement, models.AccessEntitlement.id.in_(enrollment_ids))
    data['access_entitlements'] = entitlements
    identity_ids = [r['access_identity_id'] for r in entitlements]
    data['access_identities'] = [{key:r[key] for key in ('normalized_email','provider','verified_at')}
        for r in _rows(db, models.AccessIdentity, models.AccessIdentity.id.in_(identity_ids))]
    data['report_access_grants'] = [{key:r[key] for key in ('plan_id','purpose','issued_at','expires_at','revoked_at')}
        for r in _rows(db, models.ReportAccessGrant, models.ReportAccessGrant.user_id == user_id)]
    # No bearer secrets, other roster members, provider claims or admin records.
    request.evidence = {**request.evidence, "export_prepared_at": now.isoformat(),
        "export_sha256": sha256(json.dumps(data, default=str, ensure_ascii=False, sort_keys=True).encode()).hexdigest()}
    return data


def correct_request(db, request_id, changes):
    request = owned_request(db, request_id, kind='correction')
    if request.state != 'received' or not request.user_id:
        raise ValueError('request_unavailable')
    db.query(models.User).filter_by(id=request.user_id).with_for_update().one()
    profile = db.query(models.UserProfile).filter_by(user_id=request.user_id).one()
    allowed = {'name_preference', 'communication_style'}
    if not changes or set(changes)-allowed or any(not isinstance(v,str) or len(v)>80 for v in changes.values()):
        raise ValueError('manual_review_required')
    for key, value in changes.items():
        setattr(profile, key, value)
    # Actual events/receipts/content are immutable. Identity and schedule changes
    # must use their existing owned services, never an arbitrary SQL patch.
    request.evidence = {'corrected_fields':sorted(changes)}


def complete_request(db, request_id, *, evidence_code):
    request = owned_request(db, request_id)
    if request.kind == 'deletion' or request.state != 'received':
        raise ValueError('request_unavailable')
    if evidence_code not in {'delivered_to_verified_subject','correction_confirmed','manual_resolution_delivered'}:
        raise ValueError('evidence_required')
    if evidence_code == 'delivered_to_verified_subject' and not request.evidence.get('export_sha256'):
        raise ValueError('export_evidence_required')
    if evidence_code == 'correction_confirmed' and not request.evidence.get('corrected_fields'):
        raise ValueError('correction_evidence_required')
    request.evidence = {**request.evidence, 'result':evidence_code}
    request.state = 'completed'; request.completed_at = utcnow()
    request.expires_at = request.completed_at + RETENTION


def retained_messages(db, user_id, limit=20, *, now=None):
    now = now or utcnow()
    # This is always read from durable truth. A Redis hit cannot hide a later
    # commit, correction, expiry or deletion; bounded queries use the user index.
    rows = (db.query(models.ChatHistory).join(models.User)
        .filter(models.ChatHistory.user_id == user_id, models.User.is_active.is_(True),
            models.ChatHistory.expires_at > now, models.ChatHistory.created_at > now-RETENTION)
        .order_by(models.ChatHistory.created_at.desc(), models.ChatHistory.id.desc()).limit(limit).all())
    return [{'role':r.role,'text':r.text,'created_at':r.created_at.isoformat(),
        'expires_at':r.expires_at.isoformat()} for r in reversed(rows)]


def purge_retained_data(db, *, batch_size=500, now=None):
    now = now or utcnow()
    batch_size = max(1, min(batch_size,500))
    ids = [r[0] for r in db.query(models.ChatHistory.id).filter(
        models.ChatHistory.expires_at <= now)
        .order_by(models.ChatHistory.expires_at, models.ChatHistory.id).limit(batch_size).with_for_update(skip_locked=True).all()]
    # Feedback owns an exact source; remove its content with the expired source.
    db.query(models.FeedbackEvent).filter(or_(models.FeedbackEvent.coach_message_id.in_(ids),
        models.FeedbackEvent.source_message_id.in_(ids))).delete(synchronize_session=False)
    if ids:
        db.query(models.ChatHistory).filter(models.ChatHistory.id.in_(ids)).delete(synchronize_session=False)
    feedback_ids = db.query(models.FeedbackEvent.id).filter(models.FeedbackEvent.created_at <= now-RETENTION).limit(batch_size)
    db.query(models.FeedbackEvent).filter(models.FeedbackEvent.id.in_(feedback_ids)).delete(synchronize_session=False)
    receipt_ids = db.query(models.PrivacyRequest.id).filter(models.PrivacyRequest.state == 'completed', models.PrivacyRequest.expires_at <= now).limit(batch_size)
    db.query(models.PrivacyRequest).filter(models.PrivacyRequest.id.in_(receipt_ids)).delete(synchronize_session=False)
    return len(ids)


def begin_account_deletion(db, request_id):
    hint = owned_request(db, request_id, kind='deletion')
    if hint.state == 'completed':
        return None
    user = db.query(models.User).filter_by(id=hint.user_id).with_for_update().first()
    db.refresh(hint)
    if hint.state == 'completed':
        return None
    if user is None:
        raise ValueError('deletion_subject_missing')
    enrollments = db.query(models.DeploymentEnrollment).filter_by(user_id=user.id).all()
    from app.access import lock_deployment
    for deployment_id in sorted({e.deployment_id for e in enrollments}):
        lock_deployment(db, deployment_id, require_active_period=False)
    hint.state = 'deleting'
    user.is_active = False
    db.query(models.ReportAccessGrant).filter_by(user_id=user.id).filter(
        models.ReportAccessGrant.revoked_at.is_(None)).update(
        {'revoked_at':utcnow(), 'revocation_reason':'account_deletion'}, synchronize_session=False)
    return user.id, [r[0] for r in db.query(models.AIPlan.id).filter_by(user_id=user.id)]


async def delete_account(request_id, *, memory, cancel_jobs, session_factory=None):
    """Repeatable erasure; failed external cleanup leaves the durable fence.

    Already claimed network I/O may finish; no new claim can begin after the
    fence commit. In-flight callbacks cannot recreate a missing user.
    """
    import asyncio
    session_factory = session_factory or models.SessionLocal

    def fence():
        with session_factory() as db:
            result = begin_account_deletion(db, request_id)
            if result is not None:
                # Commit attempt advancement before external I/O; a failed
                # case cannot monopolize the bounded worker after restart.
                owned_request(db, request_id).last_attempted_at = utcnow()
            db.commit()
            return result
    result = await asyncio.to_thread(fence)
    if result is None:
        return
    user_id, plan_ids = result
    # These operations must confirm success; retry preserves the same case id.
    await memory.delete_user(user_id)
    await asyncio.to_thread(cancel_jobs, user_id, plan_ids)

    def erase():
        with session_factory() as db:
            user = db.query(models.User).filter_by(id=user_id).with_for_update().first()
            request = owned_request(db, request_id, kind='deletion')
            db.refresh(request)
            if request.state == 'completed':
                return
            if user is None or user.is_active:
                raise ValueError('deletion_fence_missing')
            _erase_personal_rows(db, user_id)
            for row in db.query(models.PrivacyRequest).filter_by(user_id=user_id):
                row.user_id = None; row.source_operation_id = None
                row.state = 'completed'; row.completed_at = utcnow()
                row.evidence = {'result':'account_erased'}; row.expires_at = utcnow()+RETENTION
            db.flush()
            db.execute(models.User.__table__.delete().where(models.User.id == user_id))
            db.commit()
    await asyncio.to_thread(erase)


def _erase_personal_rows(db, user_id):
    # Explicit ownership inventory; no generic cascading scan or trigger bypass.
    plan_ids = db.query(models.AIPlan.id).filter_by(user_id=user_id)
    days = db.query(models.AIPlanDay.id).filter(models.AIPlanDay.plan_id.in_(plan_ids))
    steps = db.query(models.AIPlanStep.id).filter(models.AIPlanStep.day_id.in_(days))
    drafts = db.query(models.PlanDraftRecord.id).filter_by(user_id=user_id)
    instances = db.query(models.PlanInstance.id).filter_by(user_id=user_id)
    windows = db.query(models.PlanExecutionWindow.id).filter(models.PlanExecutionWindow.instance_id.in_(instances))
    enrollments = db.query(models.DeploymentEnrollment).filter_by(user_id=user_id).all()
    ent_ids = {e.entitlement_id for e in enrollments}
    identity_ids = {r[0] for r in db.query(models.AccessEntitlement.access_identity_id).filter(models.AccessEntitlement.id.in_(ent_ids))}
    for model in (models.FeedbackEvent, models.UserEvent, models.AggregateRecord,
        models.ReportAccessGrant, models.FailureSignal, models.TaskStats):
        db.execute(model.__table__.delete().where(model.user_id == user_id))
    for model, predicate in (
        (models.PlanCompletion, models.PlanCompletion.plan_id.in_(plan_ids)),
        (models.ExerciseDelivery, models.ExerciseDelivery.plan_step_id.in_(steps)),
        (models.PlanLifecycleOperation, models.PlanLifecycleOperation.user_id == user_id),
        (models.AIPlanVersion, models.AIPlanVersion.plan_id.in_(plan_ids)),
        (models.AIPlanStep, models.AIPlanStep.day_id.in_(days)),
        (models.AIPlanDay, models.AIPlanDay.plan_id.in_(plan_ids)),
        (models.AIPlan, models.AIPlan.user_id == user_id),
        (models.PlanDraftStep, models.PlanDraftStep.draft_id.in_(drafts)),
        (models.PlanDraftRecord, models.PlanDraftRecord.user_id == user_id),
        (models.PlanExecutionWindow, models.PlanExecutionWindow.id.in_(windows)),
        (models.PlanInstance, models.PlanInstance.user_id == user_id),
        (models.NoticeAcknowledgement, models.NoticeAcknowledgement.user_id == user_id),
        (models.OnboardingProgress, models.OnboardingProgress.user_id == user_id),
        (models.UserProfile, models.UserProfile.user_id == user_id),
        (models.ChatHistory, models.ChatHistory.user_id == user_id),
        (models.DeploymentEnrollment, models.DeploymentEnrollment.user_id == user_id),
    ):
        db.execute(model.__table__.delete().where(predicate))
    # Erase access identity only when no other account owns it. Roster totals
    # remain historical counts; never recompute/seal analytics during deletion.
    for identity_id in identity_ids:
        all_ents = db.query(models.AccessEntitlement.id).filter_by(access_identity_id=identity_id)
        shared = db.query(models.DeploymentEnrollment.id).filter(models.DeploymentEnrollment.entitlement_id.in_(all_ents)).first()
        if shared:
            # This is now the remaining account's access authority. The erased
            # subject has no enrollment/join left; do not erase another account.
            continue
        identity = db.get(models.AccessIdentity, identity_id)
        db.query(models.AccessProof).filter(models.AccessProof.identity_digest == identity.identity_digest).delete(synchronize_session=False)
        db.query(models.DeploymentInvitation).filter(models.DeploymentInvitation.entitlement_id.in_(all_ents)).delete(synchronize_session=False)
        db.query(models.AccessEntitlement).filter_by(access_identity_id=identity_id).delete(synchronize_session=False)
        db.query(models.DeploymentRosterEntry).filter_by(access_identity_id=identity_id).delete(synchronize_session=False)
        db.query(models.AccessIdentity).filter_by(id=identity_id).delete(synchronize_session=False)

"""Durable claim and retry for automatic successor creation."""

import logging
from datetime import datetime, timedelta, timezone

from app.db import PlanContinuationAttempt, User
from app.lifecycle import (
    LifecycleEntitlementError,
    get_current_plan,
    is_completion_ready,
    require_lifecycle_entitlement,
)
from app.plan_completion.chronology import utc

logger = logging.getLogger(__name__)
CLAIM_TTL = timedelta(minutes=2)
RETRY_DELAY = timedelta(minutes=5)


def prepare_automatic_continuation(session_factory, user_id, build, *, expected_plan_id=None, now=None):
    """Commit the claim before building. The lifecycle transaction remains idempotent."""
    now = now or datetime.now(timezone.utc)
    with session_factory() as db:
        if db.get(User, user_id) is None:
            raise LifecycleEntitlementError("user_not_entitled")
        require_lifecycle_entitlement(db, user_id)
        plan = get_current_plan(db, user_id, lock=True)
        if plan is None or (expected_plan_id is not None and plan.id != expected_plan_id):
            return None
        if not is_completion_ready(db, plan):
            return None
        plan_id = plan.id
        row = db.get(PlanContinuationAttempt, plan_id)
        if row is not None and row.retry_at is not None and utc(row.retry_at) > now:
            return None
        if row is None:
            row = PlanContinuationAttempt(source_plan_id=plan_id, attempt=0)
            db.add(row)
        row.attempt += 1
        row.started_at = now
        row.retry_at = now + CLAIM_TTL
        row.state = "started"
        row.failure_code = None
        attempt = row.attempt
        db.commit()

    try:
        with session_factory() as db:
            user = db.get(User, user_id)
            if user is None:
                raise LifecycleEntitlementError("user_not_entitled")
            require_lifecycle_entitlement(db, user_id)
            row = db.get(PlanContinuationAttempt, plan_id)
            if row is None or row.attempt != attempt or row.state != "started":
                return None
            result = build(db, user, expected_plan_id=plan_id)
            row.state = "created" if result is not None else "superseded"
            row.retry_at = None
            db.commit()
            return result
    except Exception as exc:
        try:
            with session_factory() as db:
                row = db.get(PlanContinuationAttempt, plan_id, with_for_update=True)
                if row is not None and row.attempt == attempt and row.state == "started":
                    row.state = "retry_wait"
                    row.failure_code = _failure_code(exc)
                    row.retry_at = datetime.now(timezone.utc) + RETRY_DELAY
                    db.commit()
        except Exception:
            logger.exception("Continuation retry update failed plan=%s", plan_id)
        raise


def read_continuation_status(db, plan_id, now):
    row = db.get(PlanContinuationAttempt, plan_id)
    if row is None or row.state == "superseded":
        return "awaiting_attempt"
    if row.state == "started" and utc(row.retry_at) <= now:
        return "attempt_unconfirmed"
    return row.state


def _failure_code(exc):
    from sqlalchemy.exc import SQLAlchemyError
    from app.plan_drafts.plan_builder_v5 import NoCandidatesError
    from app.time_slots import TimeSlotError

    if isinstance(exc, NoCandidatesError):
        return "content_unavailable"
    if isinstance(exc, TimeSlotError):
        return "invalid_saved_schedule"
    if isinstance(exc, LifecycleEntitlementError):
        return "access_changed"
    if isinstance(exc, SQLAlchemyError):
        return "storage_error"
    return "preparation_failed"

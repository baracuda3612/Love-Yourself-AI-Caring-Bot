"""Canonical, allow-listed, idempotent event ingestion primitives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any, Mapping

import pytz
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import settings
from app.db import (
    AIPlan,
    AIPlanDay,
    AIPlanStep,
    AggregateRecord,
    ContentLibrary,
    Deployment,
    DeploymentEnrollment,
    EventCatalog,
    User,
    UserEvent,
)


SKIP_STREAK_EVENT_TYPES = {"task_skipped", "task_ignored", "task_failed"}
SKIP_STREAK_RESET_EVENT_TYPES = {"task_completed"}
_BANNED_PROPERTY_KEYS = {
    "description",
    "email",
    "error",
    "exception",
    "message",
    "phone",
    "prompt",
    "response",
    "text",
    "tg_id",
    "title",
    "user_id",
    "username",
}
_MAX_SOURCE_OPERATION_LENGTH = 160
_MAX_PROPERTY_STRING_LENGTH = 160
_CONTRIBUTION_RETENTION = timedelta(days=90)
_ALLOWED_AGGREGATE_DIMENSION_KEYS = {
    "deployment_id",
    "environment",
    "event_kind",
    "event_name",
    "organization_id",
}


class EventValidationError(ValueError):
    """Raised before persistence when an event violates the catalogue envelope."""


class EventOperationConflict(RuntimeError):
    """Raised when a source operation is reused for a different fact."""


@dataclass(frozen=True)
class EventWriteResult:
    event: UserEvent
    contribution: AggregateRecord
    duplicate: bool


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _runtime_environment() -> str:
    return "production" if settings.ENVIRONMENT == "prod" else "testnet"


def _resolve_timezone(user: User) -> pytz.BaseTzInfo:
    try:
        return pytz.timezone(user.timezone or "UTC")
    except pytz.UnknownTimeZoneError:
        return pytz.UTC


def _time_bucket(local_dt: datetime) -> str:
    hour = local_dt.hour
    if hour >= 23 or hour < 6:
        return "night"
    if hour < 12:
        return "morning"
    if hour < 18:
        return "day"
    return "evening"


def _value_matches_schema(value: Any, schema_type: str) -> bool:
    if schema_type.endswith("_or_null") and value is None:
        return True
    base_type = schema_type.removesuffix("_or_null")
    if base_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if base_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if base_type == "boolean":
        return isinstance(value, bool)
    if base_type == "string":
        return isinstance(value, str) and len(value) <= _MAX_PROPERTY_STRING_LENGTH
    if base_type == "array":
        return isinstance(value, list) and len(value) <= 20
    return False


def _reject_sensitive_nested_properties(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).lower() in _BANNED_PROPERTY_KEYS:
                raise EventValidationError(f"Property {key!r} is not allowed in events")
            _reject_sensitive_nested_properties(nested)
        return
    if isinstance(value, list):
        for nested in value:
            _reject_sensitive_nested_properties(nested)
        return
    if isinstance(value, str) and len(value) > _MAX_PROPERTY_STRING_LENGTH:
        raise EventValidationError("Event property strings are bounded to 160 characters")
    if value is not None and not isinstance(value, (str, int, float, bool)):
        raise EventValidationError("Event properties must be JSON scalars, objects, or arrays")


def _validate_properties(
    properties: Mapping[str, Any], allowed_schema: Mapping[str, str]
) -> dict[str, Any]:
    normalized = dict(properties)
    unexpected = set(normalized) - set(allowed_schema)
    if unexpected:
        raise EventValidationError(
            "Properties are not allow-listed for this event: "
            + ", ".join(sorted(unexpected))
        )
    for key, value in normalized.items():
        if key.lower() in _BANNED_PROPERTY_KEYS:
            raise EventValidationError(f"Property {key!r} is not allowed in events")
        if not _value_matches_schema(value, str(allowed_schema[key])):
            raise EventValidationError(f"Property {key!r} has the wrong catalogue type")
        _reject_sensitive_nested_properties(value)
    return normalized


def _canonical_dimensions(dimensions: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    normalized = dict(dimensions)
    unexpected = set(normalized) - _ALLOWED_AGGREGATE_DIMENSION_KEYS
    if unexpected:
        raise EventValidationError(
            "Aggregate dimensions are not allow-listed: "
            + ", ".join(sorted(unexpected))
        )
    if any(isinstance(value, (Mapping, list)) for value in normalized.values()):
        raise EventValidationError("Aggregate dimensions must use coarse scalar values")
    _reject_sensitive_nested_properties(normalized)
    encoded = json.dumps(
        normalized,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(encoded) > 2048:
        raise EventValidationError("Aggregate dimensions exceed the bounded envelope")
    return normalized, sha256(encoded.encode("utf-8")).hexdigest()


def _resolve_plan_linkage(
    db: Session,
    *,
    user_id: int,
    plan_id: int | None,
    plan_step_id: int | None,
) -> tuple[int | None, int | None, str | None, int | None]:
    if plan_step_id is not None:
        row = db.execute(
            select(AIPlanStep, AIPlanDay.plan_id, AIPlan.user_id)
            .join(AIPlanDay, AIPlanDay.id == AIPlanStep.day_id)
            .join(AIPlan, AIPlan.id == AIPlanDay.plan_id)
            .where(AIPlanStep.id == plan_step_id)
        ).first()
        if row is None:
            raise EventValidationError("plan_step_id does not exist")
        step, resolved_plan_id, owner_id = row
        if owner_id != user_id:
            raise EventValidationError("plan_step_id does not belong to the user")
        if plan_id is not None and plan_id != resolved_plan_id:
            raise EventValidationError("plan_id and plan_step_id refer to different plans")
        exercise_id = step.exercise_id
        content_version = None
        if exercise_id is not None:
            content = db.get(ContentLibrary, exercise_id)
            if content is None or content.content_version <= 0:
                raise EventValidationError("plan step content identity is not valid")
            content_version = content.content_version
        return resolved_plan_id, plan_step_id, exercise_id, content_version

    if plan_id is not None:
        plan = db.get(AIPlan, plan_id)
        if plan is None or plan.user_id != user_id:
            raise EventValidationError("plan_id does not belong to the user")
    return plan_id, None, None, None


def _resolve_deployment_linkage(
    db: Session,
    *,
    user_id: int,
    deployment_id: int | None,
    deployment_enrollment_id: int | None,
) -> tuple[int | None, int | None, int | None, str]:
    if deployment_enrollment_id is not None:
        enrollment = db.get(DeploymentEnrollment, deployment_enrollment_id)
        if enrollment is None or enrollment.user_id != user_id:
            raise EventValidationError("deployment enrollment does not belong to the user")
        if deployment_id is not None and deployment_id != enrollment.deployment_id:
            raise EventValidationError("deployment and enrollment linkage disagree")
        deployment_id = enrollment.deployment_id

    organization_id = None
    environment = _runtime_environment()
    if deployment_id is not None:
        deployment = db.get(Deployment, deployment_id)
        if deployment is None:
            raise EventValidationError("deployment_id does not exist")
        if deployment.environment != environment:
            raise EventValidationError("deployment belongs to another runtime environment")
        organization_id = deployment.organization_id
    return organization_id, deployment_id, deployment_enrollment_id, environment


def _load_duplicate(
    db: Session,
    *,
    event_source: str,
    source_operation_id: str,
    event_name: str,
) -> tuple[UserEvent, AggregateRecord] | None:
    event = db.execute(
        select(UserEvent).where(
            UserEvent.event_source == event_source,
            UserEvent.source_operation_id == source_operation_id,
            UserEvent.event_name == event_name,
        )
    ).scalar_one_or_none()
    if event is None:
        return None
    contribution = db.execute(
        select(AggregateRecord).where(
            AggregateRecord.record_kind == "contribution",
            AggregateRecord.source_operation_id == source_operation_id,
        )
    ).scalar_one_or_none()
    if contribution is None:
        raise EventOperationConflict(
            "Existing event has no atomic aggregate contribution receipt"
        )
    return event, contribution


def _validate_duplicate_payload(
    duplicate: tuple[UserEvent, AggregateRecord],
    *,
    user_id: int,
    plan_id: int | None,
    plan_step_id: int | None,
    deployment_id: int | None,
    deployment_enrollment_id: int | None,
    properties: Mapping[str, Any],
    dimension_key: str,
    aggregate_value: int | float | Decimal,
) -> EventWriteResult:
    event, contribution = duplicate
    if (
        event.user_id != user_id
        or event.plan_id != plan_id
        or event.plan_step_id != plan_step_id
        or event.deployment_id != deployment_id
        or event.deployment_enrollment_id != deployment_enrollment_id
        or event.properties != dict(properties)
        or contribution.dimension_key != dimension_key
        or contribution.numeric_value != Decimal(str(aggregate_value))
    ):
        raise EventOperationConflict(
            "source_operation_id was already used for a different fact"
        )
    return EventWriteResult(event=event, contribution=contribution, duplicate=True)


def write_event_operation(
    db: Session,
    *,
    user_id: int,
    event_name: str,
    event_source: str,
    source_operation_id: str,
    properties: Mapping[str, Any] | None = None,
    occurred_at: datetime | None = None,
    plan_id: int | None = None,
    plan_step_id: int | None = None,
    deployment_id: int | None = None,
    deployment_enrollment_id: int | None = None,
    aggregate_dimensions: Mapping[str, Any] | None = None,
    aggregate_value: int | float | Decimal = 1,
) -> EventWriteResult:
    """Write one personal event and its independent contribution atomically.

    The caller owns the surrounding authoritative transaction and must commit.
    Retrying the same stable source operation returns the original pair without
    incrementing or manufacturing another fact.
    """
    event_source = event_source.strip()
    source_operation_id = source_operation_id.strip()
    if not event_source or len(event_source) > 64:
        raise EventValidationError("event_source must be 1-64 characters")
    if not source_operation_id or len(source_operation_id) > _MAX_SOURCE_OPERATION_LENGTH:
        raise EventValidationError("source_operation_id must be 1-160 characters")

    user = db.get(User, user_id)
    if user is None:
        raise EventValidationError("user does not exist")
    occurrence = occurred_at or _utc_now()
    if occurrence.tzinfo is None:
        raise EventValidationError("occurred_at must be timezone-aware")
    occurrence = occurrence.astimezone(timezone.utc)

    catalogue = db.get(EventCatalog, (event_name, 1))
    if catalogue is None:
        raise EventValidationError("event is not present in the allow-listed catalogue")
    if catalogue.activated_at > occurrence or (
        catalogue.retired_at is not None and catalogue.retired_at <= occurrence
    ):
        raise EventValidationError("event catalogue entry is not active at occurrence time")
    event_properties = _validate_properties(
        properties or {}, catalogue.allowed_property_schema
    )

    plan_id, plan_step_id, exercise_id, content_version = _resolve_plan_linkage(
        db,
        user_id=user_id,
        plan_id=plan_id,
        plan_step_id=plan_step_id,
    )
    required_linkage = set(catalogue.required_linkage)
    if "plan" in required_linkage and plan_id is None:
        raise EventValidationError("event catalogue requires plan linkage")
    if "plan_step" in required_linkage and plan_step_id is None:
        raise EventValidationError("event catalogue requires plan-step linkage")
    if required_linkage - {"user", "plan", "plan_step", "deployment"}:
        raise EventValidationError("event catalogue contains unsupported linkage")
    organization_id, deployment_id, deployment_enrollment_id, environment = (
        _resolve_deployment_linkage(
            db,
            user_id=user_id,
            deployment_id=deployment_id,
            deployment_enrollment_id=deployment_enrollment_id,
        )
    )
    if "deployment" in required_linkage and deployment_id is None:
        raise EventValidationError("event catalogue requires deployment linkage")
    local_timezone = _resolve_timezone(user)
    timezone_basis = getattr(local_timezone, "zone", "UTC") or "UTC"
    bucket = _time_bucket(occurrence.astimezone(local_timezone))

    default_dimensions = {
        "event_kind": catalogue.event_kind,
        "event_name": event_name,
        "environment": environment,
    }
    if organization_id is not None:
        default_dimensions["organization_id"] = organization_id
    if deployment_id is not None:
        default_dimensions["deployment_id"] = deployment_id
    dimensions, dimension_key = _canonical_dimensions(
        aggregate_dimensions or default_dimensions
    )
    period_start = datetime.combine(occurrence.date(), time.min, tzinfo=timezone.utc)
    period_end = period_start + timedelta(days=1)

    duplicate = _load_duplicate(
        db,
        event_source=event_source,
        source_operation_id=source_operation_id,
        event_name=event_name,
    )
    if duplicate is not None:
        return _validate_duplicate_payload(
            duplicate,
            user_id=user_id,
            plan_id=plan_id,
            plan_step_id=plan_step_id,
            deployment_id=deployment_id,
            deployment_enrollment_id=deployment_enrollment_id,
            properties=event_properties,
            dimension_key=dimension_key,
            aggregate_value=aggregate_value,
        )

    recorded_at = _utc_now()
    event_id = db.execute(
        pg_insert(UserEvent.__table__)
        .values(
            user_id=user_id,
            event_name=event_name,
            event_schema_version=1,
            occurred_at=occurrence,
            recorded_at=recorded_at,
            event_source=event_source,
            source_operation_id=source_operation_id,
            environment=environment,
            organization_id=organization_id,
            deployment_id=deployment_id,
            deployment_enrollment_id=deployment_enrollment_id,
            plan_id=plan_id,
            plan_step_id=plan_step_id,
            exercise_id=exercise_id,
            content_version=content_version,
            timezone_basis=timezone_basis,
            time_of_day_bucket=bucket,
            properties=event_properties,
        )
        .on_conflict_do_nothing(
            index_elements=["event_source", "source_operation_id", "event_name"]
        )
        .returning(UserEvent.event_id)
    ).scalar_one_or_none()
    if event_id is None:
        duplicate = _load_duplicate(
            db,
            event_source=event_source,
            source_operation_id=source_operation_id,
            event_name=event_name,
        )
        if duplicate is None:
            raise EventOperationConflict("Concurrent event receipt could not be loaded")
        return _validate_duplicate_payload(
            duplicate,
            user_id=user_id,
            plan_id=plan_id,
            plan_step_id=plan_step_id,
            deployment_id=deployment_id,
            deployment_enrollment_id=deployment_enrollment_id,
            properties=event_properties,
            dimension_key=dimension_key,
            aggregate_value=aggregate_value,
        )

    contribution_id = db.execute(
        pg_insert(AggregateRecord.__table__)
        .values(
            record_kind="contribution",
            metric_name=event_name,
            metric_schema_version=1,
            period_start=period_start,
            period_end=period_end,
            dimension_key=dimension_key,
            dimensions=dimensions,
            numeric_value=Decimal(str(aggregate_value)),
            sample_count=1,
            user_id=user_id,
            source_operation_id=source_operation_id,
            retention_until=occurrence + _CONTRIBUTION_RETENTION,
            revision=1,
        )
        .on_conflict_do_nothing(
            index_elements=["source_operation_id"],
            index_where=AggregateRecord.record_kind == "contribution",
        )
        .returning(AggregateRecord.id)
    ).scalar_one_or_none()
    if contribution_id is None:
        raise EventOperationConflict(
            "source_operation_id already owns another aggregate contribution"
        )

    db.execute(
        update(User)
        .where(User.id == user_id, User.first_seen_at.is_(None))
        .values(first_seen_at=occurrence, first_seen_at_source="accepted_event")
    )
    event = db.get(UserEvent, event_id)
    contribution = db.get(AggregateRecord, contribution_id)
    if event is None or contribution is None:
        raise EventOperationConflict("Atomic event result could not be loaded")
    return EventWriteResult(event=event, contribution=contribution, duplicate=False)


def log_user_event(
    db: Session,
    user_id: int,
    event_type: str,
    *,
    source_operation_id: str,
    event_source: str,
    plan_step_id: int | None = None,
    plan_id: int | None = None,
    context: Mapping[str, Any] | None = None,
    deployment_id: int | None = None,
    deployment_enrollment_id: int | None = None,
    step_id: str | None = None,
    content_id: str | None = None,
    plan_instance_id: str | None = None,
) -> UserEvent:
    """Compatibility name for current callers, backed only by the new operation."""
    if step_id is not None or content_id is not None or plan_instance_id is not None:
        raise EventValidationError(
            "legacy step/content/plan-instance identifiers are not accepted"
        )
    return write_event_operation(
        db,
        user_id=user_id,
        event_name=event_type,
        event_source=event_source,
        source_operation_id=source_operation_id,
        properties=context,
        plan_id=plan_id,
        plan_step_id=plan_step_id,
        deployment_id=deployment_id,
        deployment_enrollment_id=deployment_enrollment_id,
    ).event


def get_success_streak(db: Session, user_id: int, limit: int = 60) -> int:
    events = (
        db.query(UserEvent.event_name)
        .filter(
            UserEvent.user_id == user_id,
            UserEvent.event_name.in_(
                {"task_completed", "task_skipped", "task_ignored", "task_failed"}
            ),
        )
        .order_by(UserEvent.occurred_at.desc(), UserEvent.event_id.desc())
        .limit(limit)
        .all()
    )
    streak = 0
    for (event_name,) in events:
        if event_name != "task_completed":
            break
        streak += 1
    return streak


def get_skip_streak(db: Session, user_id: int, limit: int) -> int:
    events = (
        db.query(UserEvent.event_name)
        .filter(
            UserEvent.user_id == user_id,
            UserEvent.event_name.in_(
                SKIP_STREAK_EVENT_TYPES | SKIP_STREAK_RESET_EVENT_TYPES
            ),
        )
        .order_by(UserEvent.occurred_at.desc(), UserEvent.event_id.desc())
        .limit(limit)
        .all()
    )
    skip_streak = 0
    for (event_name,) in events:
        if event_name in SKIP_STREAK_RESET_EVENT_TYPES:
            break
        if event_name in SKIP_STREAK_EVENT_TYPES:
            skip_streak += 1
    return skip_streak

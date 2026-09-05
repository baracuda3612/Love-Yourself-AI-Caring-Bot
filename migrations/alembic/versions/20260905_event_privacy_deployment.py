"""Establish event, privacy, and deployment primitives.

Revision ID: 20260905_event_privacy
Revises: 20260902_plan_lifecycle
Create Date: 2026-09-05

The revision is additive around deployment/privacy authorities and switches new
event writes to a canonical envelope.  Legacy event rows remain identifiable by
a null ``event_name`` and are never assigned invented linkage or chronology.
The on-demand occurrence authority is deliberately deferred to WP-06.1.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260905_event_privacy"
down_revision = "20260902_plan_lifecycle"
branch_labels = None
depends_on = None


EVENT_CATALOGUE: tuple[tuple[str, str, dict[str, str]], ...] = (
    ("user_message", "user_behavior", {"message_length": "integer"}),
    (
        "schedule_adjustment",
        "user_behavior",
        {
            "changes": "array",
            "total_affected_steps": "integer",
            "from_day": "integer",
            "plan_id": "integer",
        },
    ),
    ("plan_activated", "user_behavior", {"total_days": "integer"}),
    (
        "plan_completed",
        "operational",
        {
            "total_days": "integer",
            "focus": "string_or_null",
            "load": "string_or_null",
            "duration": "string_or_null",
            "completion_rate": "number_or_null",
            "adaptation_count": "integer",
            "metrics_error": "boolean",
        },
    ),
    ("plan_completion_sent", "operational", {"outcome_tier": "string"}),
    (
        "plan_paused",
        "user_behavior",
        {"adaptation_type": "string", "effective_from": "string"},
    ),
    (
        "plan_resumed",
        "user_behavior",
        {"adaptation_type": "string", "effective_from": "string"},
    ),
    ("task_delivered", "operational", {"day_number": "integer"}),
    (
        "task_delivery_failed",
        "operational",
        {"day_number": "integer", "failure_code": "string"},
    ),
    ("task_completed", "user_behavior", {"day_number": "integer"}),
    ("task_skipped", "user_behavior", {"day_number": "integer"}),
    ("task_ignored", "operational", {"detection_source": "string"}),
    ("task_delayed", "user_behavior", {"reason_code": "string_or_null"}),
    ("task_failed", "operational", {"reason_code": "string_or_null"}),
    ("task_viewed_resource", "user_behavior", {"resource_kind": "string"}),
    (
        "pulse_sent",
        "operational",
        {"persona": "string", "active_day": "integer", "date": "string"},
    ),
    (
        "silent_sent",
        "operational",
        {"trigger": "string", "days_silent": "integer"},
    ),
    (
        "parameter_set",
        "user_behavior",
        {"parameter": "string", "new_value": "string_or_null"},
    ),
)


def _required_linkage(event_name: str) -> list[str]:
    if event_name.startswith("task_"):
        return ["plan_step"]
    if event_name.startswith("plan_") or event_name == "schedule_adjustment":
        return ["plan"]
    return ["user"]


def _create_enum_types() -> dict[str, postgresql.ENUM]:
    bind = op.get_bind()
    enums = {
        "deployment_environment": postgresql.ENUM(
            "testnet", "production", name="deployment_environment", create_type=False
        ),
        "deployment_timezone_mode": postgresql.ENUM(
            "single", "distributed", name="deployment_timezone_mode", create_type=False
        ),
        "roster_import_mode": postgresql.ENUM(
            "full_snapshot", "delta", name="roster_import_mode", create_type=False
        ),
        "feedback_source": postgresql.ENUM(
            "exercise_efficacy",
            "coach_quality",
            "product_feedback",
            name="feedback_source",
            create_type=False,
        ),
        "feedback_category": postgresql.ENUM(
            "bug",
            "confusion",
            "feature_request",
            "content",
            "coach",
            "other",
            name="feedback_category",
            create_type=False,
        ),
        "report_grant_purpose": postgresql.ENUM(
            "completion_report",
            "pulse_report",
            name="report_grant_purpose",
            create_type=False,
        ),
        "event_kind": postgresql.ENUM(
            "user_behavior",
            "operational",
            "access_control",
            name="event_kind",
            create_type=False,
        ),
        "aggregate_record_kind": postgresql.ENUM(
            "contribution",
            "sealed_cell",
            name="aggregate_record_kind",
            create_type=False,
        ),
    }
    for enum in enums.values():
        enum.create(bind, checkfirst=False)
    return enums


def _add_immutable_first_seen() -> None:
    # Add without a default first: PostgreSQL must not stamp migration time onto
    # legacy users.  Only row evidence below may populate the historical value.
    op.add_column(
        "users", sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "users", sa.Column("first_seen_at_source", sa.String(length=32), nullable=True)
    )
    op.execute(
        """
        WITH evidence AS (
          SELECT u.id,
                 least(
                   COALESCE((SELECT min(e.timestamp) FROM user_events e WHERE e.user_id = u.id), 'infinity'::timestamptz),
                   COALESCE((SELECT min(c.created_at) FROM chat_history c WHERE c.user_id = u.id), 'infinity'::timestamptz),
                   COALESCE((SELECT min(COALESCE(p.created_at, p.start_date)) FROM ai_plans p WHERE p.user_id = u.id), 'infinity'::timestamptz)
                 ) AS first_seen_at
          FROM users u
        ), classified AS (
          SELECT e.id, e.first_seen_at,
                 CASE
                   WHEN EXISTS (
                     SELECT 1 FROM user_events ue
                     WHERE ue.user_id = e.id AND ue.timestamp = e.first_seen_at
                   ) THEN 'legacy_event'
                   WHEN EXISTS (
                     SELECT 1 FROM chat_history ch
                     WHERE ch.user_id = e.id AND ch.created_at = e.first_seen_at
                   ) THEN 'legacy_chat'
                   ELSE 'legacy_plan'
                 END AS source
          FROM evidence e
          WHERE e.first_seen_at <> 'infinity'::timestamptz
        )
        UPDATE users u
        SET first_seen_at = c.first_seen_at,
            first_seen_at_source = c.source
        FROM classified c
        WHERE u.id = c.id
        """
    )
    op.create_check_constraint(
        "ck_users_first_seen_evidence",
        "users",
        "(first_seen_at IS NULL AND first_seen_at_source IS NULL) OR "
        "(first_seen_at IS NOT NULL AND first_seen_at_source IN "
        "('created','legacy_event','legacy_chat','legacy_plan','accepted_event'))",
    )
    op.execute("ALTER TABLE users ALTER COLUMN first_seen_at SET DEFAULT now()")
    op.execute("ALTER TABLE users ALTER COLUMN first_seen_at_source SET DEFAULT 'created'")
    op.execute(
        """
        CREATE FUNCTION ly_preserve_first_seen()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.first_seen_at IS NOT NULL AND
             (NEW.first_seen_at IS DISTINCT FROM OLD.first_seen_at OR
              NEW.first_seen_at_source IS DISTINCT FROM OLD.first_seen_at_source) THEN
            RAISE EXCEPTION 'users.first_seen_at is immutable once known';
          END IF;
          IF OLD.first_seen_at IS NULL AND NEW.first_seen_at IS NULL AND
             NEW.first_seen_at_source IS NOT NULL THEN
            RAISE EXCEPTION 'first_seen_at source requires chronology';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER tr_users_preserve_first_seen
          BEFORE UPDATE OF first_seen_at, first_seen_at_source ON users
          FOR EACH ROW EXECUTE FUNCTION ly_preserve_first_seen();
        """
    )


def _create_deployment_privacy_tables(enums: dict[str, postgresql.ENUM]) -> None:
    op.create_table(
        "privacy_notice_versions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_digest", sa.String(length=128), nullable=False),
        sa.Column("content_location", sa.String(length=512), nullable=False),
        sa.Column("is_current", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("length(btrim(version)) > 0", name="ck_privacy_notice_version"),
        sa.CheckConstraint("length(btrim(content_digest)) > 0", name="ck_privacy_notice_digest"),
        sa.CheckConstraint(
            "retired_at IS NULL OR retired_at >= published_at",
            name="ck_privacy_notice_chronology",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version", name="uq_privacy_notice_versions_version"),
    )
    op.execute(
        """
        CREATE FUNCTION ly_preserve_privacy_notice_version()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.version IS DISTINCT FROM OLD.version OR
             NEW.published_at IS DISTINCT FROM OLD.published_at OR
             NEW.content_digest IS DISTINCT FROM OLD.content_digest OR
             NEW.content_location IS DISTINCT FROM OLD.content_location THEN
            RAISE EXCEPTION 'privacy notice version content is immutable';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER tr_privacy_notice_versions_preserve_content
          BEFORE UPDATE OF version, published_at, content_digest, content_location
          ON privacy_notice_versions
          FOR EACH ROW EXECUTE FUNCTION ly_preserve_privacy_notice_version();
        """
    )
    op.create_table(
        "organizations",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("organization_key", sa.String(length=96), nullable=False),
        sa.Column("display_name", sa.String(length=160), nullable=False),
        sa.Column("commercial_metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("length(btrim(organization_key)) > 0", name="ck_organizations_key"),
        sa.CheckConstraint("jsonb_typeof(commercial_metadata) = 'object'", name="ck_organizations_metadata"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_key", name="uq_organizations_key"),
    )
    op.create_table(
        "deployments",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("organization_id", sa.BigInteger(), nullable=False),
        sa.Column("deployment_key", sa.String(length=96), nullable=False),
        sa.Column("environment", enums["deployment_environment"], nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("renewal_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enrollment_open", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("delivery_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("timezone_mode", enums["deployment_timezone_mode"], nullable=False),
        sa.Column("default_timezone", sa.String(length=64), nullable=True),
        sa.Column("notice_version_id", sa.BigInteger(), nullable=False),
        sa.Column("eligible_count_at_launch", sa.Integer(), nullable=True),
        sa.Column("champion_contact_ref", sa.String(length=160), nullable=True),
        sa.Column("support_contact_ref", sa.String(length=160), nullable=True),
        sa.Column("roster_reconciliation_days", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("starts_at IS NULL OR ends_at IS NULL OR starts_at < ends_at", name="ck_deployments_chronology"),
        sa.CheckConstraint("eligible_count_at_launch IS NULL OR eligible_count_at_launch >= 0", name="ck_deployments_eligible_count"),
        sa.CheckConstraint("roster_reconciliation_days IS NULL OR roster_reconciliation_days > 0", name="ck_deployments_reconciliation_days"),
        sa.CheckConstraint("timezone_mode <> 'single' OR length(btrim(default_timezone)) > 0", name="ck_deployments_timezone"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["notice_version_id"], ["privacy_notice_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deployment_key", name="uq_deployments_key"),
        sa.UniqueConstraint("id", "organization_id", name="uq_deployments_id_organization"),
    )
    op.execute(
        """
        CREATE FUNCTION ly_preserve_deployment_environment()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.environment IS DISTINCT FROM OLD.environment OR
             NEW.deployment_key IS DISTINCT FROM OLD.deployment_key THEN
            RAISE EXCEPTION 'deployment identity and environment are immutable';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER tr_deployments_preserve_identity
          BEFORE UPDATE OF deployment_key, environment ON deployments
          FOR EACH ROW EXECUTE FUNCTION ly_preserve_deployment_environment();
        """
    )
    op.create_table(
        "access_identities",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("identity_digest", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verification_metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("length(btrim(identity_digest)) > 0", name="ck_access_identities_digest"),
        sa.CheckConstraint("length(btrim(provider)) > 0", name="ck_access_identities_provider"),
        sa.CheckConstraint("jsonb_typeof(verification_metadata) = 'object'", name="ck_access_identities_metadata"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("identity_digest", "provider", name="uq_access_identities_digest_provider"),
    )
    op.create_table(
        "deployment_roster_versions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("deployment_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("import_mode", enums["roster_import_mode"], nullable=False),
        sa.Column("source_ref", sa.String(length=160), nullable=False),
        sa.Column("source_as_of", sa.DateTime(timezone=True), nullable=True),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("eligible_count", sa.Integer(), nullable=False),
        sa.Column("invalid_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_current", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_deployment_roster_versions_version"),
        sa.CheckConstraint("eligible_count >= 0 AND invalid_count >= 0", name="ck_deployment_roster_versions_counts"),
        sa.CheckConstraint("applied_at IS NULL OR validated_at IS NOT NULL", name="ck_deployment_roster_versions_apply"),
        sa.ForeignKeyConstraint(["deployment_id"], ["deployments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deployment_id", "version", name="uq_deployment_roster_versions_version"),
        sa.UniqueConstraint("id", "deployment_id", name="uq_deployment_roster_versions_id_deployment"),
    )
    op.create_table(
        "deployment_roster_entries",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("roster_version_id", sa.BigInteger(), nullable=False),
        sa.Column("access_identity_id", sa.BigInteger(), nullable=False),
        sa.Column("entry_key", sa.String(length=160), nullable=False),
        sa.Column("delta_action", sa.String(length=16), nullable=True),
        sa.Column("validation_metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("delta_action IS NULL OR delta_action IN ('add','remove')", name="ck_deployment_roster_entries_action"),
        sa.CheckConstraint("jsonb_typeof(validation_metadata) = 'object'", name="ck_deployment_roster_entries_metadata"),
        sa.ForeignKeyConstraint(["roster_version_id"], ["deployment_roster_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["access_identity_id"], ["access_identities.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("roster_version_id", "entry_key", name="uq_deployment_roster_entries_key"),
    )
    op.create_table(
        "access_entitlements",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("deployment_id", sa.BigInteger(), nullable=False),
        sa.Column("access_identity_id", sa.BigInteger(), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("granting_roster_version_id", sa.BigInteger(), nullable=True),
        sa.Column("confirming_roster_version_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("revoked_at IS NULL OR revoked_at >= granted_at", name="ck_access_entitlements_chronology"),
        sa.CheckConstraint("length(btrim(source)) > 0", name="ck_access_entitlements_source"),
        sa.ForeignKeyConstraint(["deployment_id"], ["deployments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["access_identity_id"], ["access_identities.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["granting_roster_version_id", "deployment_id"],
            ["deployment_roster_versions.id", "deployment_roster_versions.deployment_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["confirming_roster_version_id", "deployment_id"],
            ["deployment_roster_versions.id", "deployment_roster_versions.deployment_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "deployment_id", name="uq_access_entitlements_id_deployment"),
    )
    op.create_table(
        "deployment_invitations",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("entitlement_id", sa.BigInteger(), nullable=False),
        sa.Column("token_digest", sa.String(length=128), nullable=False),
        sa.Column("token_version", sa.Integer(), nullable=False),
        sa.Column("source_operation_id", sa.String(length=160), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("token_version > 0", name="ck_deployment_invitations_token_version"),
        sa.CheckConstraint("expires_at > issued_at", name="ck_deployment_invitations_expiry"),
        sa.CheckConstraint("redeemed_at IS NULL OR redeemed_at >= issued_at", name="ck_deployment_invitations_redeemed"),
        sa.CheckConstraint("revoked_at IS NULL OR revoked_at >= issued_at", name="ck_deployment_invitations_revoked"),
        sa.ForeignKeyConstraint(["entitlement_id"], ["access_entitlements.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_operation_id", name="uq_deployment_invitations_source_operation"),
    )
    op.create_table(
        "deployment_enrollments",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("deployment_id", sa.BigInteger(), nullable=False),
        sa.Column("entitlement_id", sa.BigInteger(), nullable=False),
        sa.Column("enrolled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_reason", sa.String(length=64), nullable=True),
        sa.Column("attribution_source", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("ended_at IS NULL OR ended_at > enrolled_at", name="ck_deployment_enrollments_chronology"),
        sa.CheckConstraint("length(btrim(attribution_source)) > 0", name="ck_deployment_enrollments_source"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["deployment_id"], ["deployments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["entitlement_id", "deployment_id"],
            ["access_entitlements.id", "access_entitlements.deployment_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "user_id", "deployment_id", name="uq_deployment_enrollments_identity"
        ),
    )
    op.create_index(
        "ix_invitations_token_digest",
        "deployment_invitations",
        ["token_digest"],
        unique=True,
    )


def _reshape_events(enums: dict[str, postgresql.ENUM]) -> None:
    op.create_unique_constraint(
        "uq_content_library_identity", "content_library", ["id", "content_version"]
    )
    op.create_unique_constraint(
        "uq_ai_plans_id_user", "ai_plans", ["id", "user_id"]
    )
    op.create_table(
        "event_catalog",
        sa.Column("event_name", sa.String(length=96), nullable=False),
        sa.Column("event_schema_version", sa.Integer(), nullable=False),
        sa.Column("event_kind", enums["event_kind"], nullable=False),
        sa.Column("allowed_property_schema", postgresql.JSONB(), nullable=False),
        sa.Column("required_linkage", postgresql.JSONB(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("event_schema_version > 0", name="ck_event_catalog_schema_version"),
        sa.CheckConstraint("length(btrim(event_name)) > 0", name="ck_event_catalog_name"),
        sa.CheckConstraint("jsonb_typeof(allowed_property_schema) = 'object'", name="ck_event_catalog_property_schema"),
        sa.CheckConstraint("jsonb_typeof(required_linkage) = 'array'", name="ck_event_catalog_required_linkage"),
        sa.CheckConstraint("retired_at IS NULL OR retired_at >= activated_at", name="ck_event_catalog_chronology"),
        sa.PrimaryKeyConstraint("event_name", "event_schema_version"),
    )
    op.execute(
        """
        CREATE FUNCTION ly_preserve_event_catalogue_definition()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.event_name IS DISTINCT FROM OLD.event_name OR
             NEW.event_schema_version IS DISTINCT FROM OLD.event_schema_version OR
             NEW.event_kind IS DISTINCT FROM OLD.event_kind OR
             NEW.allowed_property_schema IS DISTINCT FROM OLD.allowed_property_schema OR
             NEW.required_linkage IS DISTINCT FROM OLD.required_linkage OR
             NEW.activated_at IS DISTINCT FROM OLD.activated_at THEN
            RAISE EXCEPTION 'event catalogue definitions are immutable';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER tr_event_catalog_preserve_definition
          BEFORE UPDATE OF event_name, event_schema_version, event_kind,
            allowed_property_schema, required_linkage, activated_at
          ON event_catalog
          FOR EACH ROW EXECUTE FUNCTION ly_preserve_event_catalogue_definition();
        """
    )
    catalog = sa.table(
        "event_catalog",
        sa.column("event_name", sa.String()),
        sa.column("event_schema_version", sa.Integer()),
        sa.column("event_kind", enums["event_kind"]),
        sa.column("allowed_property_schema", postgresql.JSONB()),
        sa.column("required_linkage", postgresql.JSONB()),
    )
    op.bulk_insert(
        catalog,
        [
            {
                "event_name": name,
                "event_schema_version": 1,
                "event_kind": kind,
                "allowed_property_schema": schema,
                "required_linkage": _required_linkage(name),
            }
            for name, kind, schema in EVENT_CATALOGUE
        ],
    )

    op.alter_column("user_events", "id", new_column_name="event_id")
    op.drop_constraint("user_events_user_id_fkey", "user_events", type_="foreignkey")
    op.create_foreign_key(
        "user_events_user_id_fkey",
        "user_events",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.alter_column("user_events", "event_type", nullable=True)
    op.alter_column("user_events", "timestamp", nullable=True, server_default=None)
    op.alter_column("user_events", "plan_execution_id", nullable=True)
    op.alter_column("user_events", "context", nullable=True, server_default=None)
    op.drop_index("idx_user_events_context_gin", table_name="user_events")

    for column in (
        sa.Column("event_name", sa.String(length=96), nullable=True),
        sa.Column("event_schema_version", sa.Integer(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("event_source", sa.String(length=64), nullable=True),
        sa.Column("source_operation_id", sa.String(length=160), nullable=True),
        sa.Column("environment", enums["deployment_environment"], nullable=True),
        sa.Column("organization_id", sa.BigInteger(), nullable=True),
        sa.Column("deployment_id", sa.BigInteger(), nullable=True),
        sa.Column("deployment_enrollment_id", sa.BigInteger(), nullable=True),
        sa.Column("plan_id", sa.Integer(), nullable=True),
        sa.Column("plan_step_id", sa.Integer(), nullable=True),
        sa.Column("exercise_id", sa.Text(), nullable=True),
        sa.Column("content_version", sa.Integer(), nullable=True),
        sa.Column("timezone_basis", sa.String(length=64), nullable=True),
        sa.Column("properties", postgresql.JSONB(), nullable=True),
    ):
        op.add_column("user_events", column)

    op.create_foreign_key(
        "fk_user_events_catalog",
        "user_events",
        "event_catalog",
        ["event_name", "event_schema_version"],
        ["event_name", "event_schema_version"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key("fk_user_events_organization", "user_events", "organizations", ["organization_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key(
        "fk_user_events_deployment_organization",
        "user_events",
        "deployments",
        ["deployment_id", "organization_id"],
        ["id", "organization_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_user_events_enrollment_identity",
        "user_events",
        "deployment_enrollments",
        ["deployment_enrollment_id", "user_id", "deployment_id"],
        ["id", "user_id", "deployment_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_user_events_plan_owner",
        "user_events",
        "ai_plans",
        ["plan_id", "user_id"],
        ["id", "user_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key("fk_user_events_plan_step", "user_events", "ai_plan_steps", ["plan_step_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key(
        "fk_user_events_content_identity",
        "user_events",
        "content_library",
        ["exercise_id", "content_version"],
        ["id", "content_version"],
        ondelete="RESTRICT",
        match="FULL",
    )
    op.create_check_constraint(
        "ck_user_events_canonical_envelope",
        "user_events",
        "(event_name IS NULL AND event_schema_version IS NULL AND occurred_at IS NULL "
        "AND recorded_at IS NULL AND event_source IS NULL AND source_operation_id IS NULL "
        "AND environment IS NULL AND properties IS NULL) OR "
        "(event_name IS NOT NULL AND event_schema_version IS NOT NULL "
        "AND occurred_at IS NOT NULL AND recorded_at IS NOT NULL "
        "AND event_source IS NOT NULL AND length(btrim(event_source)) > 0 "
        "AND source_operation_id IS NOT NULL AND length(btrim(source_operation_id)) > 0 "
        "AND environment IS NOT NULL AND properties IS NOT NULL "
        "AND jsonb_typeof(properties) = 'object' AND recorded_at >= occurred_at)",
    )
    op.create_check_constraint(
        "ck_user_events_content_identity",
        "user_events",
        "(exercise_id IS NULL) = (content_version IS NULL) AND "
        "(content_version IS NULL OR content_version > 0)",
    )
    op.create_check_constraint(
        "ck_user_events_deployment_identity",
        "user_events",
        "deployment_id IS NULL OR organization_id IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_user_events_enrollment_identity",
        "user_events",
        "deployment_enrollment_id IS NULL OR deployment_id IS NOT NULL",
    )
    op.create_unique_constraint(
        "uq_user_events_source_operation",
        "user_events",
        ["event_source", "source_operation_id", "event_name"],
    )
    op.create_index("ix_user_events_user_time", "user_events", ["user_id", sa.text("occurred_at DESC"), "event_id"])
    op.create_index("ix_user_events_plan_time", "user_events", ["plan_id", "occurred_at", "event_id"])
    op.create_index("ix_user_events_step_time", "user_events", ["plan_step_id", "occurred_at", "event_id"])
    op.create_index("ix_user_events_deployment_time", "user_events", ["deployment_id", "occurred_at", "event_id"])
    op.execute(
        """
        CREATE FUNCTION ly_validate_user_event_catalogue()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          property_schema jsonb;
          required_links jsonb;
          item record;
          expected_type text;
          actual_type text;
        BEGIN
          IF NEW.event_name IS NULL THEN
            RETURN NEW;
          END IF;
          SELECT allowed_property_schema, required_linkage
          INTO property_schema, required_links
          FROM event_catalog
          WHERE event_name = NEW.event_name
            AND event_schema_version = NEW.event_schema_version
            AND activated_at <= NEW.occurred_at
            AND (retired_at IS NULL OR retired_at > NEW.occurred_at);
          IF NOT FOUND THEN
            RAISE EXCEPTION 'event catalogue entry is not active at occurrence time';
          END IF;
          IF EXISTS (
            SELECT 1 FROM jsonb_object_keys(NEW.properties) AS supplied(key)
            WHERE NOT property_schema ? supplied.key
          ) THEN
            RAISE EXCEPTION 'event property is not allow-listed';
          END IF;
          FOR item IN SELECT key, value FROM jsonb_each(NEW.properties)
          LOOP
            expected_type := property_schema ->> item.key;
            actual_type := jsonb_typeof(item.value);
            IF NOT (
              (expected_type = 'integer' AND actual_type = 'number'
                AND (item.value #>> '{}') ~ '^-?[0-9]+$') OR
              (expected_type = 'number' AND actual_type = 'number') OR
              (expected_type = 'boolean' AND actual_type = 'boolean') OR
              (expected_type = 'string' AND actual_type = 'string'
                AND length(item.value #>> '{}') <= 160) OR
              (expected_type = 'array' AND actual_type = 'array'
                AND jsonb_array_length(item.value) <= 20) OR
              (expected_type = 'integer_or_null' AND (
                actual_type = 'null' OR (actual_type = 'number'
                AND (item.value #>> '{}') ~ '^-?[0-9]+$'))) OR
              (expected_type = 'number_or_null' AND actual_type IN ('number','null')) OR
              (expected_type = 'boolean_or_null' AND actual_type IN ('boolean','null')) OR
              (expected_type = 'string_or_null' AND (
                actual_type = 'null' OR (actual_type = 'string'
                AND length(item.value #>> '{}') <= 160)))
            ) THEN
              RAISE EXCEPTION 'event property has the wrong catalogue type';
            END IF;
          END LOOP;
          IF required_links ? 'plan' AND NEW.plan_id IS NULL THEN
            RAISE EXCEPTION 'event catalogue requires plan linkage';
          END IF;
          IF required_links ? 'plan_step' AND NEW.plan_step_id IS NULL THEN
            RAISE EXCEPTION 'event catalogue requires plan-step linkage';
          END IF;
          IF required_links ? 'deployment' AND NEW.deployment_id IS NULL THEN
            RAISE EXCEPTION 'event catalogue requires deployment linkage';
          END IF;
          IF NEW.plan_step_id IS NOT NULL AND NOT EXISTS (
            SELECT 1
            FROM ai_plan_steps s
            JOIN ai_plan_days d ON d.id = s.day_id
            JOIN ai_plans p ON p.id = d.plan_id
            WHERE s.id = NEW.plan_step_id
              AND p.id = NEW.plan_id
              AND p.user_id = NEW.user_id
          ) THEN
            RAISE EXCEPTION 'event plan-step linkage does not belong to plan/user';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER tr_user_events_validate_catalogue
          BEFORE INSERT OR UPDATE OF event_name, event_schema_version,
            occurred_at, properties, user_id, plan_id, plan_step_id,
            deployment_id, deployment_enrollment_id, organization_id
          ON user_events
          FOR EACH ROW EXECUTE FUNCTION ly_validate_user_event_catalogue();
        CREATE FUNCTION ly_preserve_canonical_user_event()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.event_name IS NOT NULL THEN
            RAISE EXCEPTION 'canonical user events are immutable';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER tr_user_events_preserve_canonical
          BEFORE UPDATE ON user_events
          FOR EACH ROW EXECUTE FUNCTION ly_preserve_canonical_user_event();
        """
    )


def _create_feedback_report_aggregate_tables(enums: dict[str, postgresql.ENUM]) -> None:
    op.create_table(
        "feedback_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("source", enums["feedback_source"], nullable=False),
        sa.Column("source_operation_id", sa.String(length=160), nullable=False),
        sa.Column("plan_step_id", sa.Integer(), nullable=True),
        sa.Column("coach_message_id", sa.BigInteger(), nullable=True),
        sa.Column("source_message_id", sa.BigInteger(), nullable=True),
        sa.Column("value", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=160), nullable=True),
        sa.Column("category", enums["feedback_category"], nullable=True),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("context", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("length(btrim(value)) > 0", name="ck_feedback_events_value"),
        sa.CheckConstraint("jsonb_typeof(context) = 'object'", name="ck_feedback_events_context"),
        sa.CheckConstraint(
            "(source = 'exercise_efficacy' AND plan_step_id IS NOT NULL AND coach_message_id IS NULL) OR "
            "(source = 'coach_quality' AND plan_step_id IS NULL AND coach_message_id IS NOT NULL) OR "
            "(source = 'product_feedback' AND plan_step_id IS NULL AND coach_message_id IS NULL AND source_message_id IS NOT NULL)",
            name="ck_feedback_events_target",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["plan_step_id"], ["ai_plan_steps.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["coach_message_id"], ["chat_history.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_message_id"], ["chat_history.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "source_operation_id", name="uq_feedback_events_source_operation"),
    )
    op.execute(
        """
        CREATE FUNCTION ly_validate_feedback_target_ownership()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.source = 'exercise_efficacy' AND NOT EXISTS (
            SELECT 1
            FROM ai_plan_steps s
            JOIN ai_plan_days d ON d.id = s.day_id
            JOIN ai_plans p ON p.id = d.plan_id
            WHERE s.id = NEW.plan_step_id
              AND s.step_status = 'completed'
              AND p.user_id = NEW.user_id
          ) THEN
            RAISE EXCEPTION 'exercise feedback target is not a completed step owned by user';
          END IF;
          IF NEW.source = 'coach_quality' AND NOT EXISTS (
            SELECT 1 FROM chat_history c
            WHERE c.id = NEW.coach_message_id
              AND c.user_id = NEW.user_id
              AND c.role = 'assistant'
          ) THEN
            RAISE EXCEPTION 'coach feedback target is not an assistant message owned by user';
          END IF;
          IF NEW.source = 'product_feedback' AND NOT EXISTS (
            SELECT 1 FROM chat_history c
            WHERE c.id = NEW.source_message_id
              AND c.user_id = NEW.user_id
              AND c.role = 'user'
          ) THEN
            RAISE EXCEPTION 'product feedback source is not a user message owned by user';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER tr_feedback_events_validate_target_ownership
          BEFORE INSERT OR UPDATE OF user_id, source, plan_step_id,
            coach_message_id, source_message_id
          ON feedback_events
          FOR EACH ROW EXECUTE FUNCTION ly_validate_feedback_target_ownership();
        """
    )
    op.create_index("ix_feedback_user_time", "feedback_events", ["user_id", sa.text("created_at DESC")])
    op.create_table(
        "notice_acknowledgements",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("deployment_id", sa.BigInteger(), nullable=False),
        sa.Column("notice_version_id", sa.BigInteger(), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_operation_id", sa.String(length=160), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["deployment_id"], ["deployments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["notice_version_id"], ["privacy_notice_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_operation_id", name="uq_notice_ack_source_operation"),
        sa.UniqueConstraint("user_id", "deployment_id", "notice_version_id", name="uq_notice_ack_identity"),
    )
    op.execute(
        """
        CREATE FUNCTION ly_validate_notice_acknowledgement()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'UPDATE' THEN
            RAISE EXCEPTION 'notice acknowledgements are immutable';
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM deployments d
            WHERE d.id = NEW.deployment_id
              AND d.notice_version_id = NEW.notice_version_id
          ) THEN
            RAISE EXCEPTION 'acknowledged notice is not pinned to deployment';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER tr_notice_acknowledgements_validate
          BEFORE INSERT OR UPDATE ON notice_acknowledgements
          FOR EACH ROW EXECUTE FUNCTION ly_validate_notice_acknowledgement();
        """
    )
    op.create_table(
        "report_access_grants",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("purpose", enums["report_grant_purpose"], nullable=False),
        sa.Column("token_digest", sa.String(length=128), nullable=False),
        sa.Column("token_version", sa.Integer(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revocation_reason", sa.String(length=160), nullable=True),
        sa.CheckConstraint("token_version > 0", name="ck_report_access_grants_token_version"),
        sa.CheckConstraint("expires_at > issued_at", name="ck_report_access_grants_expiry"),
        sa.CheckConstraint("revoked_at IS NULL OR revoked_at >= issued_at", name="ck_report_access_grants_revocation"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["plan_id", "user_id"],
            ["ai_plans.id", "ai_plans.user_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest", name="uq_report_access_grants_token_digest"),
    )
    op.create_table(
        "aggregate_records",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("record_kind", enums["aggregate_record_kind"], nullable=False),
        sa.Column("metric_name", sa.String(length=96), nullable=False),
        sa.Column("metric_schema_version", sa.Integer(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dimension_key", sa.String(length=64), nullable=False),
        sa.Column("dimensions", postgresql.JSONB(), nullable=False),
        sa.Column("numeric_value", sa.Numeric(18, 6), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("source_operation_id", sa.String(length=160), nullable=True),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("gate_eligible_count", sa.Integer(), nullable=True),
        sa.Column("gate_contributor_count", sa.Integer(), nullable=True),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("supersedes_record_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("length(btrim(metric_name)) > 0", name="ck_aggregate_records_metric"),
        sa.CheckConstraint("metric_schema_version > 0 AND revision > 0", name="ck_aggregate_records_versions"),
        sa.CheckConstraint("period_end > period_start", name="ck_aggregate_records_period"),
        sa.CheckConstraint("length(dimension_key) = 64 AND jsonb_typeof(dimensions) = 'object'", name="ck_aggregate_records_dimensions"),
        sa.CheckConstraint("sample_count >= 0", name="ck_aggregate_records_sample_count"),
        sa.CheckConstraint(
            "(record_kind = 'contribution' AND user_id IS NOT NULL AND source_operation_id IS NOT NULL "
            "AND retention_until IS NOT NULL AND sealed_at IS NULL AND gate_eligible_count IS NULL "
            "AND gate_contributor_count IS NULL AND supersedes_record_id IS NULL AND revision = 1) OR "
            "(record_kind = 'sealed_cell' AND user_id IS NULL AND source_operation_id IS NULL "
            "AND retention_until IS NULL AND sealed_at IS NOT NULL AND gate_eligible_count IS NOT NULL "
            "AND gate_contributor_count IS NOT NULL AND gate_eligible_count >= 0 "
            "AND gate_contributor_count >= 0)",
            name="ck_aggregate_records_kind_shape",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["supersedes_record_id"], ["aggregate_records.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_aggregate_contribution_source",
        "aggregate_records",
        ["source_operation_id"],
        unique=True,
        postgresql_where=sa.text("record_kind = 'contribution'"),
    )
    op.create_index(
        "uq_aggregate_sealed_cell_revision",
        "aggregate_records",
        ["metric_name", "metric_schema_version", "period_start", "period_end", "dimension_key", "revision"],
        unique=True,
        postgresql_where=sa.text("record_kind = 'sealed_cell'"),
    )
    op.create_index(
        "uq_aggregate_sealed_supersedes",
        "aggregate_records",
        ["supersedes_record_id"],
        unique=True,
        postgresql_where=sa.text("supersedes_record_id IS NOT NULL"),
    )
    op.create_index(
        "ix_aggregate_records_cell",
        "aggregate_records",
        ["metric_name", "metric_schema_version", "period_start", "period_end", "dimension_key"],
    )
    op.execute(
        """
        CREATE FUNCTION ly_preserve_aggregate_records()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' AND OLD.record_kind = 'sealed_cell' THEN
            RAISE EXCEPTION 'sealed aggregate cells are immutable';
          END IF;
          IF TG_OP = 'UPDATE' THEN
            RAISE EXCEPTION 'aggregate records are immutable';
          END IF;
          RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END $$;
        CREATE TRIGGER tr_aggregate_records_immutable
          BEFORE UPDATE OR DELETE ON aggregate_records
          FOR EACH ROW EXECUTE FUNCTION ly_preserve_aggregate_records();
        """
    )


def upgrade() -> None:
    op.execute("SET LOCAL TIME ZONE 'UTC'")
    enums = _create_enum_types()
    _add_immutable_first_seen()
    _create_deployment_privacy_tables(enums)
    _reshape_events(enums)
    _create_feedback_report_aggregate_tables(enums)


def downgrade() -> None:
    raise RuntimeError(
        "The event/privacy/deployment authority migration is forward-only and cannot be downgraded."
    )

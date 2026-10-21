"""Complete the existing access authority; no inferred legacy grants.

Revision ID: 20261015_access_authority
Revises: 20261014_plan_completion
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261015_access_authority"
down_revision = "20261014_plan_completion"
branch_labels = None
depends_on = None


def upgrade():
    # A truthful pre-send fence prevents duplicate silent outreach when the
    # Telegram outcome is unknown; it is distinct from a confirmed send.
    op.execute(sa.text("""
        INSERT INTO event_catalog (event_name, event_schema_version, event_kind,
            allowed_property_schema, required_linkage)
        VALUES ('silent_send_attempted', 1, 'operational'::event_kind,
            '{"trigger":"string","days_silent":"integer"}'::jsonb,
            '["user"]'::jsonb)
    """))
    # Non-null legacy timezone values do not prove an explicit user choice.
    op.add_column("users", sa.Column("timezone_override_explicit", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("user_events", sa.Column("attribution_at", sa.DateTime(timezone=True)))
    op.create_check_constraint("ck_event_attribution_time", "user_events",
        "attribution_at IS NULL OR (occurred_at IS NOT NULL AND (attribution_at <= occurred_at "
        "OR (event_name IN ('task_delivered','plan_completion_sent') "
        "AND attribution_at < occurred_at + INTERVAL '1 second')) "
        "AND (attribution_at = occurred_at OR event_name IN "
        "('task_delivered','task_delivery_failed','plan_completion_sent','pulse_sent','silent_sent')))")
    # Keep actual receipt occurrence time; membership/commercial coverage is
    # checked at its authorized send claim. All other catalogue/link/immutability
    # checks remain byte-for-byte the installed predecessor implementation.
    definition = op.get_bind().execute(sa.text(
        "SELECT pg_get_functiondef('ly_validate_user_event_catalogue()'::regprocedure)"
    )).scalar_one()
    for original in (
        "e.enrolled_at <= NEW.occurred_at", "e.ended_at > NEW.occurred_at",
        "d.starts_at <= NEW.occurred_at", "d.ends_at > NEW.occurred_at",
    ):
        if original not in definition:
            raise RuntimeError("Unexpected event attribution validator; explicit forward repair required")
        definition = definition.replace(original, original.replace("NEW.occurred_at", "COALESCE(NEW.attribution_at, NEW.occurred_at)"))
    op.execute(definition)
    op.add_column("deployments", sa.Column("access_policy", postgresql.JSONB(), nullable=False, server_default="{}"))
    op.add_column("access_identities", sa.Column("normalized_email", sa.String(254)))
    op.create_index("ux_access_identity_email", "access_identities", ["normalized_email"], unique=True,
                    postgresql_where=sa.text("normalized_email IS NOT NULL"))
    for column in (
        sa.Column("base_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("preview_counts", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("confirmation_digest", sa.String(128)),
        sa.Column("preview_expires_at", sa.DateTime(timezone=True)),
        sa.Column("operator_digest", sa.String(128)),
    ):
        op.add_column("deployment_roster_versions", column)
    # Ambiguous existing authorities require explicit remediation, never guesses.
    for name, table, cols, predicate in (
        ("ux_deployment_roster_versions_one_current", "deployment_roster_versions", ["deployment_id"], "is_current"),
        ("ux_access_entitlements_one_open", "access_entitlements", ["deployment_id", "access_identity_id"], "revoked_at IS NULL"),
        ("ux_deployment_enrollments_one_active_user", "deployment_enrollments", ["user_id"], "ended_at IS NULL"),
        ("ux_deployment_enrollments_one_active_entitlement", "deployment_enrollments", ["entitlement_id"], "ended_at IS NULL"),
    ):
        op.create_index(name, table, cols, unique=True, postgresql_where=sa.text(predicate))
    op.create_index("ix_invitations_expiry", "deployment_invitations", ["expires_at", "id"],
                    postgresql_where=sa.text("redeemed_at IS NULL AND revoked_at IS NULL"))
    op.create_table("access_proofs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("deployment_id", sa.BigInteger(), sa.ForeignKey("deployments.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("secret_digest", sa.String(128), nullable=False, unique=True),
        sa.Column("browser_digest", sa.String(128), nullable=False),
        sa.Column("identity_digest", sa.String(128)),
        sa.Column("nonce_digest", sa.String(128)),
        sa.Column("subject_digest", sa.String(128)),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("kind IN ('email','google','microsoft','verified')", name="ck_access_proof_kind"),
        sa.CheckConstraint("expires_at > issued_at AND (consumed_at IS NULL OR consumed_at >= issued_at)", name="ck_access_proof_time"),
    )
    op.create_index("ix_access_proofs_expiry", "access_proofs", ["expires_at"])
    op.create_table("runtime_access_binding",
        sa.Column("singleton", sa.Integer(), primary_key=True),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("runtime_id", sa.String(96), nullable=False),
        sa.Column("resource_digest", sa.String(128), nullable=False),
        sa.CheckConstraint("singleton = 1 AND environment IN ('testnet','production')", name="ck_runtime_binding"),
    )
    op.create_table("access_admin_operations",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("deployment_id", sa.BigInteger(), sa.ForeignKey("deployments.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("operator_digest", sa.String(128), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("target_id", sa.BigInteger()),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("action IN ('founder_grant','revoke','end_enrollment','replace_identity','controls')", name="ck_access_admin_action"),
    )
    op.execute("""
        CREATE FUNCTION ly_preserve_runtime_binding() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'runtime binding is immutable'; END; $$;
        CREATE TRIGGER tr_runtime_binding_immutable BEFORE UPDATE OR DELETE ON runtime_access_binding
        FOR EACH ROW EXECUTE FUNCTION ly_preserve_runtime_binding();
        CREATE TRIGGER tr_access_admin_immutable BEFORE UPDATE OR DELETE ON access_admin_operations
        FOR EACH ROW EXECUTE FUNCTION ly_preserve_runtime_binding();
    """)


def downgrade():
    raise RuntimeError("Access authority is forward-only; restore or rebuild a disposable database.")

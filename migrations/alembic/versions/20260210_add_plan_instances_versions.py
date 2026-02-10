"""Align plan_instances schema with ORM version fields.

Revision ID: 20260210_add_plan_instances_versions
Revises: 20250330_add_plan_instance_versions
Create Date: 2026-02-10
"""

from alembic import op
import sqlalchemy as sa


revision = "20260210_add_plan_instances_versions"
down_revision = "20250330_add_plan_instance_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {column["name"] for column in inspector.get_columns("plan_instances")}

    if "contract_version" not in existing_columns:
        op.add_column(
            "plan_instances",
            sa.Column(
                "contract_version",
                sa.String(),
                nullable=False,
                server_default=sa.text("'v1'"),
            ),
        )

    if "schema_version" not in existing_columns:
        op.add_column(
            "plan_instances",
            sa.Column(
                "schema_version",
                sa.String(),
                nullable=False,
                server_default=sa.text("'v1'"),
            ),
        )

    # Keep backward compatibility with rows created before version fields existed.
    op.execute("UPDATE plan_instances SET contract_version = 'v1' WHERE contract_version IS NULL")
    op.execute("UPDATE plan_instances SET schema_version = 'v1' WHERE schema_version IS NULL")

    # Align initial_parameters with ORM expectation of non-null JSON object.
    op.execute("UPDATE plan_instances SET initial_parameters = '{}'::jsonb WHERE initial_parameters IS NULL")
    op.alter_column(
        "plan_instances",
        "initial_parameters",
        existing_type=sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    )


def downgrade() -> None:
    op.drop_column("plan_instances", "schema_version")
    op.drop_column("plan_instances", "contract_version")

"""add publication failure recovery provenance

Revision ID: a6c1d9e4f2b7
Revises: f0a1b2c3d4e5
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "a6c1d9e4f2b7"
down_revision = "f0a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "public_publication_requests",
        sa.Column(
            "recovered_from_request_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_public_publication_requests_recovered_from",
        "public_publication_requests",
        "public_publication_requests",
        ["recovered_from_request_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_public_publication_requests_recovered_from_request_id",
        "public_publication_requests",
        ["recovered_from_request_id"],
    )
    op.create_index(
        "uq_public_publication_requests_recovery_generation",
        "public_publication_requests",
        ["recovered_from_request_id", "created_main_sha"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_public_publication_requests_recovery_generation",
        table_name="public_publication_requests",
    )
    op.drop_index(
        "ix_public_publication_requests_recovered_from_request_id",
        table_name="public_publication_requests",
    )
    op.drop_constraint(
        "fk_public_publication_requests_recovered_from",
        "public_publication_requests",
        type_="foreignkey",
    )
    op.drop_column("public_publication_requests", "recovered_from_request_id")

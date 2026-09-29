"""add transition-aware public projection baseline

Revision ID: b9e2c4d6f8a0
Revises: d1e2f3a4b5c6
"""

from alembic import op
import sqlalchemy as sa


revision = "b9e2c4d6f8a0"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "public_projection_publications",
        sa.Column(
            "projection_version",
            sa.String(length=120),
            server_default="public-projection-v1.legacy",
            nullable=False,
        ),
    )
    op.add_column(
        "public_projection_publications",
        sa.Column(
            "hash_algorithm_version",
            sa.String(length=80),
            server_default="sha256-canonical-json-v1",
            nullable=False,
        ),
    )
    op.alter_column(
        "public_projection_publications",
        "projection_version",
        server_default=None,
    )
    op.alter_column(
        "public_projection_publications",
        "hash_algorithm_version",
        server_default=None,
    )
    op.add_column(
        "public_projection_publications",
        sa.Column(
            "is_published",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("public_projection_publications", "is_published")
    op.drop_column("public_projection_publications", "hash_algorithm_version")
    op.drop_column("public_projection_publications", "projection_version")

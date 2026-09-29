"""Allow transition-aware public release record counts.

Revision ID: public_0002
Revises: public_0001
"""

from alembic import op


revision = "public_0002"
down_revision = "public_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_public_release_record_count",
        "public_releases",
        type_="check",
    )
    op.create_check_constraint(
        "ck_public_release_record_count",
        "public_releases",
        "record_count BETWEEN 1 AND 10000",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_public_release_record_count",
        "public_releases",
        type_="check",
    )
    op.create_check_constraint(
        "ck_public_release_record_count",
        "public_releases",
        "record_count = 40",
    )

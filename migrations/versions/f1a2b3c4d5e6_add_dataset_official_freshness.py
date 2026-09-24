"""store official dataset validity separately

Revision ID: f1a2b3c4d5e6
Revises: e1f2a3b4c5d6
Create Date: 2026-09-24
"""

from alembic import op
import sqlalchemy as sa


revision = "f1a2b3c4d5e6"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "data_sets",
        sa.Column("official_actual_until", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("data_sets", "official_actual_until")

"""add npd status checks

Revision ID: a41c9f2e7b60
Revises: 9d6b2f4a7c31
Create Date: 2026-09-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a41c9f2e7b60"

down_revision: Union[
    str,
    Sequence[str],
    None,
] = "9d6b2f4a7c31"

branch_labels: Union[
    str,
    Sequence[str],
    None,
] = None

depends_on: Union[
    str,
    Sequence[str],
    None,
] = None


def upgrade() -> None:
    op.create_table(
        "npd_status_checks",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(),
            nullable=False,
        ),
        sa.Column(
            "inn",
            sa.String(length=12),
            nullable=False,
        ),
        sa.Column(
            "request_date",
            sa.Date(),
            nullable=False,
        ),
        sa.Column(
            "result_status",
            sa.String(length=30),
            nullable=False,
        ),
        sa.Column(
            "is_npd",
            sa.Boolean(),
            nullable=True,
        ),
        sa.Column(
            "message",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "http_status",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column(
            "error_code",
            sa.String(length=120),
            nullable=True,
        ),
        sa.Column(
            "checked_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "inn",
            "request_date",
            name="uq_npd_status_inn_request_date",
        ),
    )

    op.create_index(
        op.f("ix_npd_status_checks_inn"),
        "npd_status_checks",
        ["inn"],
        unique=False,
    )
    op.create_index(
        op.f("ix_npd_status_checks_request_date"),
        "npd_status_checks",
        ["request_date"],
        unique=False,
    )
    op.create_index(
        op.f("ix_npd_status_checks_result_status"),
        "npd_status_checks",
        ["result_status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_npd_status_checks_error_code"),
        "npd_status_checks",
        ["error_code"],
        unique=False,
    )
    op.create_index(
        op.f("ix_npd_status_checks_checked_at"),
        "npd_status_checks",
        ["checked_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_npd_status_checks_checked_at"),
        table_name="npd_status_checks",
    )
    op.drop_index(
        op.f("ix_npd_status_checks_error_code"),
        table_name="npd_status_checks",
    )
    op.drop_index(
        op.f("ix_npd_status_checks_result_status"),
        table_name="npd_status_checks",
    )
    op.drop_index(
        op.f("ix_npd_status_checks_request_date"),
        table_name="npd_status_checks",
    )
    op.drop_index(
        op.f("ix_npd_status_checks_inn"),
        table_name="npd_status_checks",
    )
    op.drop_table("npd_status_checks")

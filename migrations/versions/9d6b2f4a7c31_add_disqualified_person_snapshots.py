"""add disqualified person snapshots

Revision ID: 9d6b2f4a7c31
Revises: 627b6a78b250
Create Date: 2026-09-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9d6b2f4a7c31"

down_revision: Union[
    str,
    Sequence[str],
    None,
] = "627b6a78b250"

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
        "disqualified_person_snapshots",

        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=False),
            nullable=False,
        ),

        sa.Column(
            "dataset_id",
            sa.BigInteger(),
            nullable=False,
        ),

        sa.Column(
            "data_date",
            sa.Date(),
            nullable=False,
        ),

        sa.Column(
            "register_number",
            sa.String(length=32),
            nullable=False,
        ),

        sa.Column(
            "full_name",
            sa.String(length=500),
            nullable=False,
        ),

        sa.Column(
            "birth_date",
            sa.Date(),
            nullable=True,
        ),

        sa.Column(
            "birth_place",
            sa.String(length=1000),
            nullable=True,
        ),

        sa.Column(
            "organization_name",
            sa.String(length=1000),
            nullable=True,
        ),

        sa.Column(
            "organization_inn",
            sa.String(length=12),
            nullable=True,
        ),

        sa.Column(
            "position",
            sa.String(length=500),
            nullable=True,
        ),

        sa.Column(
            "offence_article",
            sa.String(length=500),
            nullable=True,
        ),

        sa.Column(
            "protocol_authority",
            sa.String(length=1000),
            nullable=True,
        ),

        sa.Column(
            "judge_name",
            sa.String(length=500),
            nullable=True,
        ),

        sa.Column(
            "judge_position",
            sa.String(length=500),
            nullable=True,
        ),

        sa.Column(
            "disqualification_term",
            sa.String(length=100),
            nullable=True,
        ),

        sa.Column(
            "start_date",
            sa.Date(),
            nullable=True,
        ),

        sa.Column(
            "end_date",
            sa.Date(),
            nullable=True,
        ),

        sa.Column(
            "created_at",
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

        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["data_sets.id"],
            ondelete="CASCADE",
        ),

        sa.PrimaryKeyConstraint(
            "id"
        ),

        sa.UniqueConstraint(
            "dataset_id",
            "data_date",
            "register_number",
            name=(
                "uq_disqualified_person_"
                "dataset_date_register"
            ),
        ),
    )

    op.create_index(
        op.f(
            "ix_disqualified_person_"
            "snapshots_dataset_id"
        ),
        "disqualified_person_snapshots",
        ["dataset_id"],
        unique=False,
    )

    op.create_index(
        op.f(
            "ix_disqualified_person_"
            "snapshots_data_date"
        ),
        "disqualified_person_snapshots",
        ["data_date"],
        unique=False,
    )

    op.create_index(
        op.f(
            "ix_disqualified_person_"
            "snapshots_register_number"
        ),
        "disqualified_person_snapshots",
        ["register_number"],
        unique=False,
    )

    op.create_index(
        op.f(
            "ix_disqualified_person_"
            "snapshots_full_name"
        ),
        "disqualified_person_snapshots",
        ["full_name"],
        unique=False,
    )

    op.create_index(
        op.f(
            "ix_disqualified_person_"
            "snapshots_organization_inn"
        ),
        "disqualified_person_snapshots",
        ["organization_inn"],
        unique=False,
    )

    op.create_index(
        op.f(
            "ix_disqualified_person_"
            "snapshots_start_date"
        ),
        "disqualified_person_snapshots",
        ["start_date"],
        unique=False,
    )

    op.create_index(
        op.f(
            "ix_disqualified_person_"
            "snapshots_end_date"
        ),
        "disqualified_person_snapshots",
        ["end_date"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f(
            "ix_disqualified_person_"
            "snapshots_end_date"
        ),
        table_name=(
            "disqualified_person_snapshots"
        ),
    )

    op.drop_index(
        op.f(
            "ix_disqualified_person_"
            "snapshots_start_date"
        ),
        table_name=(
            "disqualified_person_snapshots"
        ),
    )

    op.drop_index(
        op.f(
            "ix_disqualified_person_"
            "snapshots_organization_inn"
        ),
        table_name=(
            "disqualified_person_snapshots"
        ),
    )

    op.drop_index(
        op.f(
            "ix_disqualified_person_"
            "snapshots_full_name"
        ),
        table_name=(
            "disqualified_person_snapshots"
        ),
    )

    op.drop_index(
        op.f(
            "ix_disqualified_person_"
            "snapshots_register_number"
        ),
        table_name=(
            "disqualified_person_snapshots"
        ),
    )

    op.drop_index(
        op.f(
            "ix_disqualified_person_"
            "snapshots_data_date"
        ),
        table_name=(
            "disqualified_person_snapshots"
        ),
    )

    op.drop_index(
        op.f(
            "ix_disqualified_person_"
            "snapshots_dataset_id"
        ),
        table_name=(
            "disqualified_person_snapshots"
        ),
    )

    op.drop_table(
        "disqualified_person_snapshots"
    )

"""correct V1 worker counters, fencing and timeout foundation

Revision ID: c9d0e1f2a3b4
Revises: b7c8d9e0f1a2
Create Date: 2026-09-23
"""

from alembic import op
import sqlalchemy as sa


revision = "c9d0e1f2a3b4"
down_revision = "b7c8d9e0f1a2"
branch_labels = None
depends_on = None


COUNTER_COLUMNS = (
    "records_seen",
    "records_written",
    "records_rejected",
    "records_duplicated",
    "records_published",
)


def upgrade() -> None:
    for name in COUNTER_COLUMNS:
        op.add_column(
            "worker_runs",
            sa.Column(name, sa.BigInteger(), server_default="0", nullable=False),
        )
    op.create_check_constraint(
        "ck_worker_runs_counters",
        "worker_runs",
        "records_seen >= 0 AND records_written >= 0 "
        "AND records_rejected >= 0 AND records_duplicated >= 0 "
        "AND records_published >= 0",
    )

    # Sequence allocation is independent of worker_leases row lifetime and is
    # intentionally non-transactional: failed/conflicting claims may create
    # gaps, but no token can ever be reused after a lease row is deleted.
    op.execute("CREATE SEQUENCE worker_lease_fencing_token_seq AS BIGINT")
    op.execute(
        "SELECT setval("
        "'worker_lease_fencing_token_seq', "
        "GREATEST("
        "COALESCE((SELECT MAX(fencing_token) FROM worker_leases), 0), "
        "COALESCE((SELECT MAX(fencing_token) FROM worker_runs), 0)"
        ") + 1, false"
        ")"
    )


def downgrade() -> None:
    op.execute("DROP SEQUENCE worker_lease_fencing_token_seq")
    op.drop_constraint("ck_worker_runs_counters", "worker_runs", type_="check")
    for name in reversed(COUNTER_COLUMNS):
        op.drop_column("worker_runs", name)

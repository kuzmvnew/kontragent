"""add data readiness foundation

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-09-17
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = (
        sa.Column("dataset_kind", sa.String(50), nullable=False, server_default="bulk_snapshot"),
        sa.Column("freshness_policy", sa.String(30), nullable=False, server_default="irregular"),
        sa.Column("freshness_threshold_seconds", sa.Integer()),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("source_as_of", sa.DateTime(timezone=True)),
        sa.Column("retrieved_at", sa.DateTime(timezone=True)),
        sa.Column("checked_at", sa.DateTime(timezone=True)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("record_count", sa.BigInteger()),
        sa.Column("coverage", postgresql.JSONB()),
        sa.Column("operational_status", sa.String(30), nullable=False, server_default="not_configured"),
        sa.Column("last_error", sa.Text()),
        sa.Column("last_error_at", sa.DateTime(timezone=True)),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True)),
        sa.Column("next_expected_update_at", sa.DateTime(timezone=True)),
        sa.Column("auto_update_status", sa.String(30), nullable=False, server_default="not_configured"),
    )
    for column in columns:
        op.add_column("data_sets", column)
    for name in (
        "dataset_kind", "freshness_policy", "operational_status", "auto_update_status",
        "next_retry_at", "next_expected_update_at",
    ):
        op.create_index(f"ix_data_sets_{name}", "data_sets", [name])

    run_columns = (
        sa.Column("run_uuid", sa.String(36)),
        sa.Column("trigger", sa.String(30)),
        sa.Column("source_as_of", sa.DateTime(timezone=True)),
        sa.Column("retrieved_at", sa.DateTime(timezone=True)),
        sa.Column("records_seen", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("records_written", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("records_rejected", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("duplicates", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("conflicts", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("version", sa.String(200)),
        sa.Column("error_code", sa.String(120)),
        sa.Column("duration_ms", sa.BigInteger()),
        sa.Column("lock_owner", sa.String(200)),
    )
    for column in run_columns:
        op.add_column("ingestion_runs", column)
    op.create_index("ix_ingestion_runs_run_uuid", "ingestion_runs", ["run_uuid"], unique=True)
    op.create_index("ix_ingestion_runs_error_code", "ingestion_runs", ["error_code"])

    op.create_table(
        "dataset_update_locks",
        sa.Column("dataset_id", sa.BigInteger(), sa.ForeignKey("data_sets.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("owner", sa.String(200), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_dataset_update_locks_owner", "dataset_update_locks", ["owner"])
    op.create_index("ix_dataset_update_locks_expires_at", "dataset_update_locks", ["expires_at"])

    op.create_table(
        "dataset_publications",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("dataset_id", sa.BigInteger(), sa.ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", sa.BigInteger(), sa.ForeignKey("ingestion_runs.id", ondelete="SET NULL")),
        sa.Column("version", sa.String(200), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source_as_of", sa.DateTime(timezone=True)),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("checksum", sa.String(64)),
        sa.Column("record_count", sa.BigInteger()),
        sa.Column("metadata_json", postgresql.JSONB()),
        sa.UniqueConstraint("dataset_id", "version", name="uq_dataset_publication_version"),
    )
    op.create_index("ix_dataset_publications_dataset_id", "dataset_publications", ["dataset_id"])
    op.create_index("ix_dataset_publications_run_id", "dataset_publications", ["run_id"])
    op.create_index("ix_dataset_publications_status", "dataset_publications", ["status"])
    op.create_index("ix_dataset_publications_is_active", "dataset_publications", ["is_active"])
    op.create_index(
        "uq_dataset_publications_one_active",
        "dataset_publications", ["dataset_id"], unique=True,
        postgresql_where=sa.text("is_active"),
    )

    # Preserve legacy cadence while making unsupported values honest.
    op.execute("""
        UPDATE data_sets SET
          dataset_kind = CASE
            WHEN update_mode IN ('bulk', 'delta', 'import') THEN 'bulk_snapshot'
            ELSE 'on_demand_api' END,
          freshness_policy = CASE
            WHEN refresh_schedule IN ('daily','weekly','monthly') THEN refresh_schedule
            WHEN refresh_schedule = 'on_demand' THEN 'on_demand'
            WHEN refresh_schedule = 'manual' THEN 'manual'
            ELSE 'irregular' END,
          auto_update_status = CASE
            WHEN refresh_schedule = 'manual' THEN 'manual'
            WHEN refresh_schedule = 'on_demand' THEN 'user_triggered'
            ELSE 'not_configured' END,
          operational_status = CASE
            WHEN refresh_schedule = 'manual' THEN 'manual'
            WHEN refresh_schedule = 'on_demand' THEN 'user_triggered'
            WHEN last_success_at IS NOT NULL THEN 'current'
            ELSE 'not_configured' END,
          source_as_of = CASE WHEN last_data_date IS NULL THEN NULL
            ELSE last_data_date::timestamp AT TIME ZONE 'UTC' END,
          retrieved_at = last_success_at,
          checked_at = last_success_at,
          published_at = last_success_at,
          coverage = jsonb_build_object(
            'note', CASE WHEN update_mode IN ('bulk','delta','import')
              THEN 'Coverage counters were not captured by the legacy run; next validated publish will populate them.'
              ELSE 'On-demand coverage is limited to explicitly checked INNs.' END
          )
    """)
    op.execute("""
        UPDATE data_sets d SET
          last_attempt_at = history.last_attempt
        FROM (
          SELECT dataset_id, max(started_at) AS last_attempt
          FROM ingestion_runs GROUP BY dataset_id
        ) history WHERE history.dataset_id=d.id
    """)
    op.execute("""
        UPDATE data_sets d SET record_count = history.written
        FROM (
          SELECT DISTINCT ON (dataset_id) dataset_id, rows_inserted + rows_updated AS written
          FROM ingestion_runs WHERE status='success' ORDER BY dataset_id, started_at DESC
        ) history WHERE history.dataset_id=d.id AND history.written IS NOT NULL
    """)
    op.execute("""
        UPDATE data_sets SET
          dataset_kind='public_live_user_triggered', freshness_policy='user_triggered',
          operational_status='user_triggered', auto_update_status='user_triggered'
        WHERE code IN ('fns_npd','checko_arbitration_cases','moscow_general_court_cases')
    """)
    op.execute("""
        UPDATE data_sets SET dataset_kind='source_blocked', operational_status='source_blocked',
          auto_update_status='source_blocked', freshness_policy='irregular'
        WHERE code IN ('rkn_broadcast_licenses','rkn_registered_media','nostroy_nrs_protected')
    """)
    op.execute("""
        UPDATE data_sets SET dataset_kind='access_credential_pending', operational_status='access_pending',
          auto_update_status='access_pending', freshness_policy='access_pending'
        WHERE code='eis_procurements'
    """)
    op.execute("""
        INSERT INTO data_sets
          (source_id, code, name, domain, update_mode, data_format, refresh_schedule, priority,
           enabled, source_url, description, dataset_kind, freshness_policy,
           operational_status, auto_update_status, retry_count, coverage)
        SELECT id, 'fns_account_suspension', 'ФНС: приостановление операций по счетам',
          'account_suspension', 'manual', 'html', 'manual', 10, true,
          'https://service.nalog.ru/bi.do', 'Human-assisted проверка; CAPTCHA не обходится.',
          'human_assisted', 'manual', 'manual', 'human_assisted', 0,
          '{"note":"Human-assisted checks cover only explicitly checked INNs."}'::jsonb
        FROM data_sources WHERE code='fns' ON CONFLICT (code) DO NOTHING
    """)
    op.execute("""
        INSERT INTO data_sets
          (source_id, code, name, domain, update_mode, data_format, refresh_schedule, priority,
           enabled, source_url, description, dataset_kind, freshness_policy,
           operational_status, auto_update_status, retry_count, coverage)
        SELECT id, 'cbr_zsk', 'Банк России: платформа ЗСК', 'know_your_customer',
          'manual', 'html', 'manual', 10, true, 'https://www.cbr.ru/counteraction_m_ter/',
          'Human-assisted проверка; challenge не обходится.', 'human_assisted', 'manual',
          'manual', 'human_assisted', 0,
          '{"note":"Human-assisted checks cover only explicitly checked INNs."}'::jsonb
          FROM data_sources WHERE code='cbr'
        ON CONFLICT (code) DO NOTHING
    """)
    op.execute("""
        INSERT INTO data_sets
          (source_id, code, name, domain, update_mode, data_format, refresh_schedule, priority,
           enabled, source_url, description, dataset_kind, freshness_policy,
           operational_status, auto_update_status, retry_count, coverage)
        SELECT id, 'eis_rnp', 'ЕИС: реестр недобросовестных поставщиков', 'procurement_exclusion',
          'api', 'json', 'access_pending', 10, false, 'https://zakupki.gov.ru/',
          'Ожидается официальный credentialed access; DaMIA автоматически не используется.',
          'access_credential_pending', 'access_pending', 'access_pending', 'access_pending', 0,
          '{"note":"Coverage cannot be measured until official access is granted."}'::jsonb
        FROM data_sources WHERE code='eis' ON CONFLICT (code) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DELETE FROM data_sets WHERE code IN ('fns_account_suspension','cbr_zsk','eis_rnp')")
    op.drop_table("dataset_publications")
    op.drop_table("dataset_update_locks")
    op.drop_index("ix_ingestion_runs_error_code", table_name="ingestion_runs")
    op.drop_index("ix_ingestion_runs_run_uuid", table_name="ingestion_runs")
    for name in (
        "lock_owner", "duration_ms", "error_code", "version", "conflicts", "duplicates",
        "records_rejected", "records_written", "records_seen", "retrieved_at", "source_as_of",
        "trigger", "run_uuid",
    ):
        op.drop_column("ingestion_runs", name)
    for name in (
        "auto_update_status", "next_expected_update_at", "next_retry_at", "retry_count",
        "last_error_at", "last_error", "operational_status", "coverage", "record_count",
        "published_at", "checked_at", "retrieved_at", "source_as_of", "last_attempt_at",
        "freshness_threshold_seconds", "freshness_policy", "dataset_kind",
    ):
        op.drop_column("data_sets", name)

#!/usr/bin/env python3
"""Archive recognized score-v3 tables and install normalized-v3 tables.

The default action is a read-only inspection.  ``apply`` is deliberately
limited to an exact, recognized legacy state and refuses the historical
``kontragent`` database unless all rollout authorization controls are supplied.
No legacy row is converted into the normalized-v3 semantic family.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from dotenv import load_dotenv
from sqlalchemy.dialects import postgresql

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.models  # noqa: E402,F401 -- register the complete canonical schema
from app.database.base import Base  # noqa: E402
from migrations.schema_validation_v1 import (  # noqa: E402
    audit_metadata,
    compare_table_schema,
    inspected_table_schema,
    model_table_schema,
)


MANIFEST_VERSION = 1
HELPER_VERSION = "DEV-005/1"
LEGACY_REVISION = "c0c90245de4b"
CANONICAL_PARENT = "a7d4e9f2c6b1"
CANONICAL_TARGET = "c8e3f1a6b904"
CURRENT_SCHEMA_HEAD = "9b7d4c2a1e80"
ARCHIVE_SCHEMA = "legacy_v3_archive"
RISK_TABLE = "company_risk_assessments_v3"
SUMMARY_TABLE = "company_summaries_v3"
POST_CANONICAL_TARGET_TABLES = {
    "mintrans_ted_raw_artifacts",
    "mintrans_ted_entries",
    "mintrans_ted_quarantine_rows",
    "transport_forwarding_registry_listings",
    "worker_jobs",
    "worker_runs",
    "worker_leases",
    "worker_handler_registry",
    "worker_raw_manifests",
    "worker_publication_state",
    "source_change_summaries",
    "admin_action_audit",
    "source_incidents",
    "source_incident_actions",
    "source_automation_policies",
    "registry_source_checkpoints",
    "company_registry_changes",
    "master_replay_signals",
    "company_enrichment_runs",
    "company_source_coverage",
    "girbo_accounting_reports",
    "fns_tax_debt_publication_generations",
    "fns_tax_debt_pilot_state",
}
DEV009_EXTENSION_TABLES = {
    "fns_tax_debt_raw_artifacts",
    "fns_tax_debt_normalized_records",
    "fns_tax_debt_quarantine_records",
}
DEV009_SNAPSHOT_EXTENSION_PATHS = {
    "normalized_record_id",
    "fact_code",
    "source_reference",
    "provenance",
    "limitation_states",
    "retrieved_at",
    "ck_company_tax_debt_fact_code",
    "ix_company_tax_debt_snapshots_normalized_record_id",
    "ix_company_tax_debt_snapshots_fact_code",
    "ix_company_tax_debt_snapshots_retrieved_at",
    "publication_generation",
    "ix_company_tax_debt_snapshots_publication_generation",
    "uq_company_tax_debt_company_dataset_date_generation",
    "uq_company_tax_debt_company_dataset_date",
}
V1_TABLES = ("company_risk_assessments", "company_summaries")
PROTECTED_DATABASE = "kontragent"
ROLLOUT_ENV = "LEGACY_V3_ROLLOUT_AUTHORIZATION"
EXPECTED_POSTGRES_MAJOR = 18


class ReconciliationBlocked(RuntimeError):
    """A fail-closed state or identity guard prevented reconciliation."""


def legacy_tables(*, schema: str = "public") -> tuple[sa.Table, sa.Table]:
    """Return the exact frozen schema emitted by historical revision a7b8..."""
    metadata = sa.MetaData()
    sa.Table(
        "companies",
        metadata,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        schema="public",
    )
    risk = sa.Table(
        RISK_TABLE,
        metadata,
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("assessment_id", sa.String(36), nullable=False, unique=True),
        sa.Column("company_id", sa.BigInteger(), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("risk_engine_version", sa.String(40), nullable=False),
        sa.Column("coverage_engine_version", sa.String(40), nullable=False),
        sa.Column("calculated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("risk_score", sa.Integer(), nullable=False),
        sa.Column("risk_label", sa.String(80), nullable=False),
        sa.Column("normalized_results", postgresql.JSONB(), nullable=False),
        sa.Column("coverage", postgresql.JSONB(), nullable=False),
        sa.Column("result_payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["company_id"], ["public.companies.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        schema=schema,
    )
    for name in (
        "assessment_id",
        "company_id",
        "input_hash",
        "risk_engine_version",
        "calculated_at",
        "risk_score",
    ):
        sa.Index(f"ix_{RISK_TABLE}_{name}", risk.c[name])

    summary = sa.Table(
        SUMMARY_TABLE,
        metadata,
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("summary_id", sa.String(36), nullable=False, unique=True),
        sa.Column("company_id", sa.BigInteger(), nullable=False),
        sa.Column("risk_assessment_id", sa.String(36), nullable=False),
        sa.Column("summary_engine_version", sa.String(40), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("structured_payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["company_id"], ["public.companies.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["risk_assessment_id"],
            [f"{schema}.{RISK_TABLE}.assessment_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("risk_assessment_id"),
        schema=schema,
    )
    for name in (
        "summary_id",
        "company_id",
        "risk_assessment_id",
        "summary_engine_version",
        "generated_at",
    ):
        sa.Index(f"ix_{SUMMARY_TABLE}_{name}", summary.c[name])
    return risk, summary


def canonical_tables() -> tuple[sa.Table, sa.Table]:
    return Base.metadata.tables[RISK_TABLE], Base.metadata.tables[SUMMARY_TABLE]


def canonical_parent_metadata() -> sa.MetaData:
    """Freeze the current parent as every canonical table except c8's two."""
    metadata = sa.MetaData()
    for table in Base.metadata.sorted_tables:
        if table.name not in {
            RISK_TABLE,
            SUMMARY_TABLE,
            *POST_CANONICAL_TARGET_TABLES,
        }:
            table.to_metadata(metadata)
    return metadata


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    raise TypeError(f"Unsupported value in deterministic serialization: {type(value)!r}")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")


def _aggregate_hash(values: Iterable[Any]) -> str:
    digest = hashlib.sha256()
    for value in values:
        encoded = _canonical_bytes(value)
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _quote(connection: sa.Connection, identifier: str) -> str:
    return connection.dialect.identifier_preparer.quote_identifier(identifier)


def _qualified(connection: sa.Connection, schema: str, table: str) -> str:
    return f"{_quote(connection, schema)}.{_quote(connection, table)}"


def database_identity(connection: sa.Connection) -> dict[str, Any]:
    server_version_num = int(connection.scalar(sa.text("SHOW server_version_num")))
    url = connection.engine.url
    return {
        "database": connection.scalar(sa.text("SELECT current_database()")),
        "server_address": connection.scalar(
            sa.text("SELECT COALESCE(inet_server_addr()::text, 'local-socket')")
        ),
        "host": url.host or "local-socket",
        "port": connection.scalar(
            sa.text(
                "SELECT COALESCE(inet_server_port(), current_setting('port')::int)"
            )
        ),
        "postgresql_version": connection.scalar(sa.text("SHOW server_version")),
        "postgresql_version_num": server_version_num,
    }


def current_revisions(connection: sa.Connection) -> list[str]:
    inspector = sa.inspect(connection)
    if not inspector.has_table("alembic_version", schema="public"):
        return []
    return sorted(
        connection.scalars(sa.text("SELECT version_num FROM public.alembic_version"))
    )


def owned_sequences(
    connection: sa.Connection, schema: str, table: str
) -> list[dict[str, Any]]:
    rows = connection.execute(
        sa.text(
            """
            SELECT a.attname AS column_name,
                   sequence_ns.nspname AS sequence_schema,
                   sequence.relname AS sequence_name,
                   dependency.deptype AS dependency_type,
                   parameters.seqstart AS start,
                   parameters.seqincrement AS increment,
                   parameters.seqmin AS minvalue,
                   parameters.seqmax AS maxvalue,
                   parameters.seqcache AS cache,
                   parameters.seqcycle AS cycle
            FROM pg_class AS target
            JOIN pg_namespace AS target_ns ON target_ns.oid = target.relnamespace
            JOIN pg_attribute AS a
              ON a.attrelid = target.oid AND a.attnum > 0 AND NOT a.attisdropped
            JOIN pg_depend AS dependency
              ON dependency.refobjid = target.oid
             AND dependency.refobjsubid = a.attnum
             AND dependency.deptype IN ('a', 'i')
            JOIN pg_class AS sequence
              ON sequence.oid = dependency.objid AND sequence.relkind = 'S'
            JOIN pg_namespace AS sequence_ns
              ON sequence_ns.oid = sequence.relnamespace
            JOIN pg_sequence AS parameters ON parameters.seqrelid = sequence.oid
            WHERE target_ns.nspname = :schema AND target.relname = :table
            ORDER BY a.attname, sequence_ns.nspname, sequence.relname
            """
        ),
        {"schema": schema, "table": table},
    ).mappings()
    return [dict(row) for row in rows]


def table_fingerprint(
    connection: sa.Connection,
    table: sa.Table,
    *,
    schema: str,
    expected_comment: str | None = None,
) -> dict[str, Any]:
    inspector = sa.inspect(connection)
    if not inspector.has_table(table.name, schema=schema):
        return {
            "table": table.name,
            "schema": schema,
            "exists": False,
            "exact": False,
            "errors": [{"kind": "missing_table", "table": table.name}],
            "observations": [],
            "comment": None,
            "owned_sequences": [],
        }
    expected = model_table_schema(table, connection.dialect)
    actual = inspected_table_schema(inspector, table.name, schema=schema)
    # SQLAlchemy/PostgreSQL reflection can erase the referred schema when the
    # target is visible on search_path, then incorrectly substitute the source
    # table's schema.  Catalog dependencies are authoritative for an archived
    # table whose company FK must continue to reference public.companies.
    foreign_key_targets = {
        tuple(row["columns"]): row["target_schema"]
        for row in connection.execute(
            sa.text(
                """
                SELECT array_agg(source_column.attname ORDER BY key_column.ordinality)
                         AS columns,
                       target_ns.nspname AS target_schema
                FROM pg_constraint AS constraint_row
                JOIN pg_class AS source_table
                  ON source_table.oid = constraint_row.conrelid
                JOIN pg_namespace AS source_ns
                  ON source_ns.oid = source_table.relnamespace
                JOIN pg_class AS target_table
                  ON target_table.oid = constraint_row.confrelid
                JOIN pg_namespace AS target_ns
                  ON target_ns.oid = target_table.relnamespace
                JOIN LATERAL unnest(constraint_row.conkey) WITH ORDINALITY
                  AS key_column(attnum, ordinality) ON true
                JOIN pg_attribute AS source_column
                  ON source_column.attrelid = source_table.oid
                 AND source_column.attnum = key_column.attnum
                WHERE constraint_row.contype = 'f'
                  AND source_ns.nspname = :schema
                  AND source_table.relname = :table
                GROUP BY constraint_row.oid, target_ns.nspname
                """
            ),
            {"schema": schema, "table": table.name},
        ).mappings()
    }
    for foreign_key in actual["foreign_keys"]:
        target_schema = foreign_key_targets.get(tuple(foreign_key["columns"]))
        if target_schema is not None:
            foreign_key["target_schema"] = target_schema
    errors, observations = compare_table_schema(expected, actual, strict=True)
    comment = actual.get("comment")
    if comment != expected_comment:
        errors.append(
            {
                "kind": "table_comment_mismatch",
                "path": "comment",
                "expected": expected_comment,
                "actual": comment,
            }
        )
    sequences = owned_sequences(connection, schema, table.name)
    expected_sequence = f"{table.name}_id_seq"
    expected_owned = {
        "column_name": "id",
        "sequence_schema": schema,
        "sequence_name": expected_sequence,
        "dependency_type": "i",
        "start": 1,
        "increment": 1,
        "minvalue": 1,
        "maxvalue": 9223372036854775807,
        "cache": 1,
        "cycle": False,
    }
    if len(sequences) != 1 or any(
        sequences[0].get(key) != value for key, value in expected_owned.items()
    ):
        errors.append(
            {
                "kind": "owned_identity_sequence_mismatch",
                "path": "id",
                "expected": [expected_owned],
                "actual": sequences,
            }
        )
    constraint_validation = [
        dict(row)
        for row in connection.execute(
            sa.text(
                """
                SELECT constraint_row.conname AS name,
                       constraint_row.contype AS type,
                       constraint_row.convalidated AS validated
                FROM pg_constraint AS constraint_row
                JOIN pg_class AS target ON target.oid = constraint_row.conrelid
                JOIN pg_namespace AS target_ns ON target_ns.oid = target.relnamespace
                WHERE target_ns.nspname = :schema AND target.relname = :table
                ORDER BY constraint_row.conname
                """
            ),
            {"schema": schema, "table": table.name},
        ).mappings()
    ]
    unvalidated = [item for item in constraint_validation if not item["validated"]]
    if unvalidated:
        errors.append(
            {
                "kind": "unvalidated_constraint",
                "path": "constraints",
                "expected": "all constraints validated",
                "actual": unvalidated,
            }
        )
    return {
        "table": table.name,
        "schema": schema,
        "exists": True,
        "exact": not errors,
        "errors": errors,
        "observations": observations,
        "comment": comment,
        "owned_sequences": sequences,
        "constraint_validation": constraint_validation,
    }


def legacy_fingerprints(
    connection: sa.Connection, *, schema: str
) -> dict[str, dict[str, Any]]:
    risk, summary = legacy_tables(schema=schema)
    return {
        "risk": table_fingerprint(connection, risk, schema=schema),
        "summary": table_fingerprint(connection, summary, schema=schema),
    }


def canonical_fingerprints(connection: sa.Connection) -> dict[str, dict[str, Any]]:
    risk, summary = canonical_tables()
    return {
        "risk": table_fingerprint(connection, risk, schema="public"),
        "summary": table_fingerprint(connection, summary, schema="public"),
    }


def full_parent_compatibility(connection: sa.Connection) -> dict[str, Any]:
    report = audit_metadata(connection, canonical_parent_metadata())
    # DEV-009/DEV-010 are forward-compatible extensions relative to the historical
    # canonical-v3 parent checked by this utility.  A legacy database may not
    # have these later objects yet; a current database may have all of them.
    extension_differences = []
    blocking_differences = []
    for error in report["errors"]:
        table = error.get("table")
        path = error.get("path")
        compatible_extension = (
            table in DEV009_EXTENSION_TABLES
            and error.get("kind") == "missing_table"
        ) or (
            table == "company_tax_debt_snapshots"
            and path in DEV009_SNAPSHOT_EXTENSION_PATHS
            and (
                error.get("kind", "").startswith("missing")
                or error.get("kind", "").startswith("unexpected")
            )
        )
        if compatible_extension:
            extension_differences.append(error)
        else:
            blocking_differences.append(error)
    if extension_differences:
        report["observations"].extend(
            {
                "kind": "compatible_post_parent_extension_absent",
                "table": item.get("table"),
                "path": item.get("path"),
            }
            for item in extension_differences
        )
    report["errors"] = blocking_differences
    report["compatible"] = not blocking_differences
    ignored = {RISK_TABLE, SUMMARY_TABLE}
    report["ignored_semantic_family_tables"] = sorted(
        ignored.intersection(report["extra_tables"])
    )
    report["compatible_extra_objects"] = report["observations"]
    report["blocking_differences"] = report["errors"]
    # The complete expected schema is useful during a live diagnosis but is
    # redundant and very large in the durable reconciliation manifest.
    report.pop("expected", None)
    report.pop("actual", None)
    return report


def _rows(
    connection: sa.Connection,
    schema: str,
    table: str,
    *,
    order_by: tuple[str, ...],
) -> list[dict[str, Any]]:
    qualified = _qualified(connection, schema, table)
    order = ", ".join(_quote(connection, column) for column in order_by)
    return [
        dict(row)
        for row in connection.execute(
            sa.text(f"SELECT * FROM {qualified} ORDER BY {order}")
        ).mappings()
    ]


def _json_key_inventory(
    connection: sa.Connection, schema: str, table: str, column: str
) -> dict[str, int]:
    qualified = _qualified(connection, schema, table)
    quoted_column = _quote(connection, column)
    rows = connection.execute(
        sa.text(
            f"""
            SELECT key, count(*) AS row_count
            FROM {qualified} AS source
            CROSS JOIN LATERAL jsonb_object_keys(
                CASE WHEN jsonb_typeof(source.{quoted_column}) = 'object'
                     THEN source.{quoted_column} ELSE '{{}}'::jsonb END
            ) AS key
            GROUP BY key ORDER BY key
            """
        )
    )
    return {key: count for key, count in rows}


def _null_profile(
    connection: sa.Connection, schema: str, table: str, columns: Iterable[str]
) -> dict[str, int]:
    qualified = _qualified(connection, schema, table)
    expressions = ", ".join(
        f"count(*) FILTER (WHERE {_quote(connection, column)} IS NULL) AS {_quote(connection, column)}"
        for column in columns
    )
    row = connection.execute(sa.text(f"SELECT {expressions} FROM {qualified}")).mappings().one()
    return dict(row)


def _distribution(
    connection: sa.Connection, schema: str, table: str, column: str
) -> list[dict[str, Any]]:
    qualified = _qualified(connection, schema, table)
    quoted = _quote(connection, column)
    rows = connection.execute(
        sa.text(
            f"SELECT {quoted} AS value, count(*) AS count "
            f"FROM {qualified} GROUP BY {quoted} ORDER BY {quoted}"
        )
    ).mappings()
    return [dict(row) for row in rows]


def _duplicate_count(
    connection: sa.Connection, schema: str, table: str, columns: tuple[str, ...]
) -> dict[str, int]:
    qualified = _qualified(connection, schema, table)
    group = ", ".join(_quote(connection, column) for column in columns)
    row = connection.execute(
        sa.text(
            f"SELECT count(*) AS groups, COALESCE(sum(n - 1), 0) AS excess_rows "
            f"FROM (SELECT count(*) AS n FROM {qualified} "
            f"GROUP BY {group} HAVING count(*) > 1) AS duplicates"
        )
    ).mappings().one()
    return {key: int(value) for key, value in row.items()}


def legacy_row_inventory(
    connection: sa.Connection, *, schema: str
) -> dict[str, Any]:
    risk_columns = tuple(column.name for column in legacy_tables(schema=schema)[0].columns)
    summary_columns = tuple(column.name for column in legacy_tables(schema=schema)[1].columns)
    risk_rows = _rows(
        connection, schema, RISK_TABLE, order_by=("id", "assessment_id")
    )
    summary_rows = _rows(
        connection, schema, SUMMARY_TABLE, order_by=("id", "summary_id")
    )
    risk_ids = [row["assessment_id"] for row in risk_rows]
    summary_ids = [row["summary_id"] for row in summary_rows]
    risk_payloads = [
        {
            "assessment_id": row["assessment_id"],
            "normalized_results": row["normalized_results"],
            "coverage": row["coverage"],
            "result_payload": row["result_payload"],
        }
        for row in risk_rows
    ]
    summary_payloads = [
        {
            "summary_id": row["summary_id"],
            "structured_payload": row["structured_payload"],
        }
        for row in summary_rows
    ]
    risk_name = _qualified(connection, schema, RISK_TABLE)
    summary_name = _qualified(connection, schema, SUMMARY_TABLE)
    cross = connection.execute(
        sa.text(
            f"""
            SELECT
              count(*) FILTER (WHERE risk.assessment_id IS NULL) AS orphan_summaries,
              count(risk.assessment_id) AS linked_summaries,
              count(*) FILTER (
                WHERE risk.assessment_id IS NOT NULL
                  AND summary.company_id <> risk.company_id
              ) AS company_mismatches
            FROM {summary_name} AS summary
            LEFT JOIN {risk_name} AS risk
              ON risk.assessment_id = summary.risk_assessment_id
            """
        )
    ).mappings().one()
    link_rows = connection.execute(
        sa.text(
            f"""
            SELECT summary.summary_id, summary.risk_assessment_id
            FROM {summary_name} AS summary
            ORDER BY summary.summary_id, summary.risk_assessment_id
            """
        )
    ).mappings()
    return {
        "risk": {
            "total_count": len(risk_rows),
            "distinct_assessment_id": len(set(risk_ids)),
            "id_inventory_sha256": _aggregate_hash(risk_ids),
            "row_sha256": _aggregate_hash(risk_rows),
            "payload_sha256": _aggregate_hash(risk_payloads),
            "risk_engine_version_distribution": _distribution(
                connection, schema, RISK_TABLE, "risk_engine_version"
            ),
            "coverage_engine_version_distribution": _distribution(
                connection, schema, RISK_TABLE, "coverage_engine_version"
            ),
            "json_key_inventory": {
                column: _json_key_inventory(
                    connection, schema, RISK_TABLE, column
                )
                for column in ("normalized_results", "coverage", "result_payload")
            },
            "null_profile": _null_profile(
                connection, schema, RISK_TABLE, risk_columns
            ),
            "duplicate_assessment_ids": _duplicate_count(
                connection, schema, RISK_TABLE, ("assessment_id",)
            ),
            "duplicate_reuse_identities": _duplicate_count(
                connection, schema, RISK_TABLE, ("company_id", "input_hash")
            ),
        },
        "summary": {
            "total_count": len(summary_rows),
            "distinct_summary_id": len(set(summary_ids)),
            "id_inventory_sha256": _aggregate_hash(summary_ids),
            "row_sha256": _aggregate_hash(summary_rows),
            "payload_sha256": _aggregate_hash(summary_payloads),
            "summary_engine_version_distribution": _distribution(
                connection, schema, SUMMARY_TABLE, "summary_engine_version"
            ),
            "json_key_inventory": {
                "structured_payload": _json_key_inventory(
                    connection, schema, SUMMARY_TABLE, "structured_payload"
                )
            },
            "null_profile": _null_profile(
                connection, schema, SUMMARY_TABLE, summary_columns
            ),
            "duplicate_summary_ids": _duplicate_count(
                connection, schema, SUMMARY_TABLE, ("summary_id",)
            ),
            "duplicate_reuse_identities": _duplicate_count(
                connection, schema, SUMMARY_TABLE, ("risk_assessment_id",)
            ),
        },
        "cross_table": {
            "orphan_summary_count": int(cross["orphan_summaries"]),
            "summary_risk_link_count": int(cross["linked_summaries"]),
            "company_mismatch_count": int(cross["company_mismatches"]),
            "link_inventory_sha256": _aggregate_hash(
                [dict(row) for row in link_rows]
            ),
        },
    }


def table_preservation_snapshot(
    connection: sa.Connection, schema: str, table: str
) -> dict[str, Any]:
    inspector = sa.inspect(connection)
    if not inspector.has_table(table, schema=schema):
        return {"exists": False, "count": 0, "row_sha256": _aggregate_hash([])}
    pk = inspector.get_pk_constraint(table, schema=schema).get("constrained_columns")
    columns = [item["name"] for item in inspector.get_columns(table, schema=schema)]
    order_by = tuple(pk or columns)
    rows = _rows(connection, schema, table, order_by=order_by)
    ids = [tuple(row[column] for column in order_by) for row in rows]
    return {
        "exists": True,
        "count": len(rows),
        "id_inventory_sha256": _aggregate_hash(ids),
        "row_sha256": _aggregate_hash(rows),
    }


def v1_snapshot(connection: sa.Connection) -> dict[str, Any]:
    return {
        table: table_preservation_snapshot(connection, "public", table)
        for table in V1_TABLES
    }


def business_table_counts(connection: sa.Connection) -> dict[str, int]:
    excluded = {"alembic_version", RISK_TABLE, SUMMARY_TABLE}
    counts: dict[str, int] = {}
    for table in sorted(sa.inspect(connection).get_table_names(schema="public")):
        if table in excluded:
            continue
        counts[table] = int(
            connection.scalar(
                sa.text(f"SELECT count(*) FROM {_qualified(connection, 'public', table)}")
            )
        )
    return counts


def _schema_exists(connection: sa.Connection, schema: str) -> bool:
    return bool(
        connection.scalar(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = :schema)"
            ),
            {"schema": schema},
        )
    )


def classify_state(connection: sa.Connection) -> dict[str, Any]:
    revisions = current_revisions(connection)
    identity = database_identity(connection)
    public_legacy = legacy_fingerprints(connection, schema="public")
    public_canonical = canonical_fingerprints(connection)
    parent = full_parent_compatibility(connection)
    archive_exists = _schema_exists(connection, ARCHIVE_SCHEMA)
    archive_tables = (
        sorted(sa.inspect(connection).get_table_names(schema=ARCHIVE_SCHEMA))
        if archive_exists
        else []
    )
    archive = (
        legacy_fingerprints(connection, schema=ARCHIVE_SCHEMA)
        if archive_exists
        else None
    )
    legacy_exact = all(item["exact"] for item in public_legacy.values())
    canonical_exact = all(item["exact"] for item in public_canonical.values())
    archive_exact = bool(
        archive
        and set(archive_tables) == {RISK_TABLE, SUMMARY_TABLE}
        and all(item["exact"] for item in archive.values())
    )

    if (
        revisions == [LEGACY_REVISION]
        and legacy_exact
        and parent["compatible"]
        and not archive_exists
    ):
        state = "ELIGIBLE_FOR_RECONCILIATION"
        blocker = None
    elif (
        revisions in ([CANONICAL_TARGET], [CURRENT_SCHEMA_HEAD])
        and canonical_exact
        and not archive_exists
    ):
        state = "NO_OP_PASS"
        blocker = None
    elif (
        revisions in ([CANONICAL_TARGET], [CURRENT_SCHEMA_HEAD])
        and canonical_exact
        and archive_exact
    ):
        state = "NO_OP_VERIFY_PASS"
        blocker = None
    else:
        partial = archive_exists or (
            public_legacy["risk"]["exact"] != public_legacy["summary"]["exact"]
        ) or (
            public_canonical["risk"]["exact"]
            != public_canonical["summary"]["exact"]
        )
        state = (
            "BLOCKED_PARTIAL_RECONCILIATION_STATE"
            if partial
            else "BLOCKED_UNRECOGNIZED_LEGACY_STATE"
        )
        blocker = {
            "revision_match": revisions
            in ([LEGACY_REVISION], [CANONICAL_TARGET], [CURRENT_SCHEMA_HEAD]),
            "legacy_exact": legacy_exact,
            "canonical_exact": canonical_exact,
            "parent_compatible": parent["compatible"],
            "archive_exists": archive_exists,
            "archive_exact": archive_exact,
        }
    return {
        "state": state,
        "blocker": blocker,
        "database_identity": identity,
        "source_revisions": revisions,
        "public_legacy_fingerprints": public_legacy,
        "public_canonical_fingerprints": public_canonical,
        "full_pre_v3_compatibility": parent,
        "archive_schema_exists": archive_exists,
        "archive_tables": archive_tables,
        "archive_fingerprints": archive,
    }


def _git_sha() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def new_manifest(action: str) -> dict[str, Any]:
    return {
        "manifest_version": MANIFEST_VERSION,
        "helper_version": HELPER_VERSION,
        "helper_git_sha": _git_sha(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "canonical_parent": CANONICAL_PARENT,
        "canonical_target": CANONICAL_TARGET,
        "result": "IN_PROGRESS",
    }


def inspect_connection(connection: sa.Connection, *, action: str) -> dict[str, Any]:
    manifest = new_manifest(action)
    state = classify_state(connection)
    manifest.update(state)
    if state["state"] == "ELIGIBLE_FOR_RECONCILIATION":
        manifest["legacy_rows"] = legacy_row_inventory(connection, schema="public")
    elif state["state"] == "NO_OP_VERIFY_PASS":
        manifest["legacy_rows"] = legacy_row_inventory(
            connection, schema=ARCHIVE_SCHEMA
        )
    manifest["v1"] = {"before": v1_snapshot(connection)}
    manifest["other_business_tables"] = {
        "before_counts": business_table_counts(connection)
    }
    manifest["result"] = state["state"]
    return manifest


def enforce_apply_guard(
    identity: dict[str, Any],
    *,
    confirm_database: str | None,
    confirm_host: str | None,
    confirm_port: int | None,
    allow_protected_database: bool,
    rollout_authorization: str | None,
) -> None:
    database = identity["database"]
    if identity["postgresql_version_num"] // 10000 != EXPECTED_POSTGRES_MAJOR:
        raise ReconciliationBlocked(
            f"PostgreSQL major mismatch: expected {EXPECTED_POSTGRES_MAJOR}, "
            f"observed {identity['postgresql_version']}"
        )
    if confirm_database != database:
        raise ReconciliationBlocked(
            "Apply requires --confirm-database matching current_database()"
        )
    if confirm_host != identity["host"]:
        raise ReconciliationBlocked(
            "Apply requires --confirm-host matching the connected URL host"
        )
    if confirm_port != identity["port"]:
        raise ReconciliationBlocked(
            "Apply requires --confirm-port matching the PostgreSQL server port"
        )
    if database == PROTECTED_DATABASE and not (
        allow_protected_database and rollout_authorization == database
    ):
        raise ReconciliationBlocked(
            f"Apply to protected database {database!r} is forbidden by default; "
            f"a future authorized rollout requires both --allow-protected-database "
            f"and {ROLLOUT_ENV}={database}"
        )


def _assert_preserved(before: Any, after: Any, label: str) -> None:
    if before != after:
        raise ReconciliationBlocked(f"Preservation verification failed: {label}")


def _stamp(
    connection: sa.Connection, revision: str, *, purge: bool = False
) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    context = MigrationContext.configure(connection)
    if purge:
        # Alembic's own version-table operation, equivalent to `stamp --purge`.
        context._ensure_version_table(purge=True)  # noqa: SLF001
    context.stamp(script, revision)


def _apply_canonical_revision(connection: sa.Connection) -> None:
    module = importlib.import_module(
        "migrations.versions.c8e3f1a6b904_add_normalized_risk_summary_v3"
    )
    with Operations.context(MigrationContext.configure(connection)):
        module.upgrade()
    _stamp(connection, CANONICAL_TARGET)


def apply_reconciliation(
    connection: sa.Connection,
    *,
    confirm_database: str | None,
    confirm_host: str | None = None,
    confirm_port: int | None = None,
    allow_protected_database: bool = False,
    rollout_authorization: str | None = None,
) -> dict[str, Any]:
    """Reconcile one connection. The caller owns the surrounding transaction."""
    manifest = new_manifest("apply")
    before_state = classify_state(connection)
    manifest.update(before_state)
    enforce_apply_guard(
        before_state["database_identity"],
        confirm_database=confirm_database,
        confirm_host=confirm_host,
        confirm_port=confirm_port,
        allow_protected_database=allow_protected_database,
        rollout_authorization=rollout_authorization,
    )
    if before_state["state"] in {"NO_OP_PASS", "NO_OP_VERIFY_PASS"}:
        if before_state["state"] == "NO_OP_VERIFY_PASS":
            manifest["legacy_rows"] = legacy_row_inventory(
                connection, schema=ARCHIVE_SCHEMA
            )
        manifest["v1"] = {"before": v1_snapshot(connection)}
        manifest["other_business_tables"] = {
            "before_counts": business_table_counts(connection)
        }
        manifest["result"] = before_state["state"]
        return manifest
    if before_state["state"] != "ELIGIBLE_FOR_RECONCILIATION":
        raise ReconciliationBlocked(before_state["state"])

    before_rows = legacy_row_inventory(connection, schema="public")
    before_v1 = v1_snapshot(connection)
    before_business = business_table_counts(connection)
    manifest["legacy_rows"] = {"before": before_rows}
    manifest["v1"] = {"before": before_v1}
    manifest["other_business_tables"] = {"before_counts": before_business}
    manifest["archive_move"] = {"verified": False}
    manifest["metadata_adoption"] = {
        "target": CANONICAL_PARENT,
        "executed_after_archive_verification": False,
        "result": "NOT_RUN",
    }
    manifest["canonical_migration"] = {
        "target": CANONICAL_TARGET,
        "result": "NOT_RUN",
    }

    connection.exec_driver_sql(f"CREATE SCHEMA {_quote(connection, ARCHIVE_SCHEMA)}")
    connection.exec_driver_sql(
        f"ALTER TABLE {_qualified(connection, 'public', RISK_TABLE)} "
        f"SET SCHEMA {_quote(connection, ARCHIVE_SCHEMA)}"
    )
    connection.exec_driver_sql(
        f"ALTER TABLE {_qualified(connection, 'public', SUMMARY_TABLE)} "
        f"SET SCHEMA {_quote(connection, ARCHIVE_SCHEMA)}"
    )
    archive_fingerprints = legacy_fingerprints(
        connection, schema=ARCHIVE_SCHEMA
    )
    if not all(item["exact"] for item in archive_fingerprints.values()):
        raise ReconciliationBlocked("Archive fingerprint verification failed")
    archived_rows = legacy_row_inventory(connection, schema=ARCHIVE_SCHEMA)
    _assert_preserved(before_rows, archived_rows, "legacy rows/hashes")
    manifest["legacy_rows"]["archive"] = archived_rows
    manifest["archive_move"] = {
        "schema": ARCHIVE_SCHEMA,
        "fingerprints": archive_fingerprints,
        "verified": True,
    }

    manifest["metadata_adoption"]["executed_after_archive_verification"] = True
    _stamp(connection, CANONICAL_PARENT, purge=True)
    if current_revisions(connection) != [CANONICAL_PARENT]:
        raise ReconciliationBlocked("Canonical parent metadata adoption failed")
    manifest["metadata_adoption"]["result"] = "PASS"

    _apply_canonical_revision(connection)
    if current_revisions(connection) != [CANONICAL_TARGET]:
        raise ReconciliationBlocked("Exact canonical target migration failed")
    manifest["canonical_migration"]["result"] = "PASS"
    manifest["canonical_migration"]["final_revision"] = current_revisions(connection)

    final_canonical = canonical_fingerprints(connection)
    final_archive = legacy_fingerprints(connection, schema=ARCHIVE_SCHEMA)
    final_complete = audit_metadata(connection, Base.metadata)
    final_complete.pop("expected", None)
    final_complete.pop("actual", None)
    if not all(item["exact"] for item in final_canonical.values()):
        raise ReconciliationBlocked("Final canonical v3 fingerprint failed")
    if not all(item["exact"] for item in final_archive.values()):
        raise ReconciliationBlocked("Final archive fingerprint failed")
    if not final_complete["compatible"]:
        raise ReconciliationBlocked("Final canonical schema completeness failed")
    after_v1 = v1_snapshot(connection)
    after_business = business_table_counts(connection)
    _assert_preserved(before_v1, after_v1, "v1 tables")
    _assert_preserved(before_business, after_business, "other business table counts")
    manifest["v1"]["after"] = after_v1
    manifest["v1"]["preserved"] = True
    manifest["other_business_tables"]["after_counts"] = after_business
    manifest["other_business_tables"]["preserved"] = True
    manifest["final_canonical_fingerprints"] = final_canonical
    manifest["final_archive_fingerprints"] = final_archive
    manifest["final_schema_completeness"] = final_complete
    manifest["result"] = "RECONCILED_PASS"
    return manifest


def _default_manifest_path(database: str, action: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_database = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in database
    )
    return Path(tempfile.gettempdir()) / (
        f"legacy-v3-reconciliation-{safe_database}-{action}-{timestamp}.json"
    )


def write_manifest(manifest: dict[str, Any], output: Path | None) -> Path:
    target = output or _default_manifest_path(
        manifest.get("database_identity", {}).get("database", "unknown"),
        manifest["action"],
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=_json_default)
        + "\n",
        encoding="utf-8",
    )
    return target


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        nargs="?",
        choices=("inspect", "apply", "verify"),
        default="inspect",
    )
    parser.add_argument("--database-url", help="Defaults to DATABASE_URL")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--confirm-database")
    parser.add_argument("--confirm-host")
    parser.add_argument("--confirm-port", type=int)
    parser.add_argument("--allow-protected-database", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_dotenv(ROOT / ".env")
    database_url = args.database_url or os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    engine = sa.create_engine(database_url)
    manifest: dict[str, Any] = new_manifest(args.action)
    output: Path | None = None
    exit_code = 0
    try:
        if args.action == "apply":
            try:
                with engine.begin() as connection:
                    manifest = apply_reconciliation(
                        connection,
                        confirm_database=args.confirm_database,
                        confirm_host=args.confirm_host,
                        confirm_port=args.confirm_port,
                        allow_protected_database=args.allow_protected_database,
                        rollout_authorization=os.getenv(ROLLOUT_ENV),
                    )
            except Exception as error:
                failure_result = (
                    "BLOCKED" if isinstance(error, ReconciliationBlocked) else "FAIL"
                )
                # The apply transaction has rolled back. Capture the durable
                # post-failure state read-only so a failed run never leaves an
                # ambiguous manifest.
                try:
                    with engine.connect() as connection:
                        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                        manifest = inspect_connection(connection, action="apply")
                        connection.rollback()
                except Exception as inspection_error:
                    manifest["post_failure_inspection_error"] = str(
                        inspection_error
                    )
                manifest["result"] = failure_result
                manifest["error"] = str(error)
                exit_code = 2
        else:
            with engine.connect() as connection:
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                manifest = inspect_connection(connection, action=args.action)
                if args.action == "verify" and manifest["state"] not in {
                    "NO_OP_PASS",
                    "NO_OP_VERIFY_PASS",
                }:
                    manifest["result"] = "VERIFY_FAILED"
                    exit_code = 2
                elif manifest["state"].startswith("BLOCKED_"):
                    exit_code = 2
                connection.rollback()
    finally:
        engine.dispose()
        output = write_manifest(manifest, args.manifest)
    print(
        json.dumps(
            {
                "result": manifest["result"],
                "state": manifest.get("state"),
                "database": manifest.get("database_identity", {}).get("database"),
                "manifest": str(output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

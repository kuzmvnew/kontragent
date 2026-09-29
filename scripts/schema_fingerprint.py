#!/usr/bin/env python3
"""Produce a deterministic PostgreSQL schema fingerprint.

The digest deliberately excludes database name, object OIDs, owners and
timestamps so fresh, upgraded and restored databases can be compared. Alembic
revision values are part of the payload but never substitute for the objects.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row


CONTRACT_VERSION = 2
EXPECTED_POSTGRESQL_VERSION = "18.6"
CATEGORIES = (
    "schemas",
    "relations",
    "columns",
    "indexes",
    "unique_constraints",
    "foreign_keys",
    "check_constraints",
    "triggers",
    "routines",
    "migration_revisions",
)


class FingerprintMismatch(RuntimeError):
    """Observed schema does not satisfy the expected structure."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def payload_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _rows(connection: psycopg.Connection, query: str) -> list[dict[str, Any]]:
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(query)
        return [dict(row) for row in cursor.fetchall()]


_STRING_LITERAL = r"'(?:''|[^'])*'"


def _canonical_sql(value: str) -> str:
    """Normalize equivalent PostgreSQL deparser output after dump/restore.

    PostgreSQL 18 can re-express varchar literal arrays by moving their
    ``::text`` cast from the array to each element. The parse trees are
    equivalent, but the raw output of pg_get_* is not byte-identical.
    Normalize only that narrow, semantics-preserving form; object names,
    operators, values and all other casts remain part of the contract.
    """
    value = re.sub(
        rf"\(({_STRING_LITERAL})::character varying\)::text",
        r"\1",
        value,
    )
    value = re.sub(
        rf"({_STRING_LITERAL})::character varying::text",
        r"\1",
        value,
    )
    value = re.sub(
        rf"({_STRING_LITERAL})::character varying",
        r"\1",
        value,
    )
    value = re.sub(
        r"\((ARRAY\[[^\]]*\])\)::text\[\]",
        r"\1",
        value,
    )
    return re.sub(
        r"(ARRAY\[[^\]]*\])::text\[\]",
        r"\1",
        value,
    )


def fingerprint_connection(connection: psycopg.Connection) -> dict[str, Any]:
    queries = {
        "schemas": """
            SELECT nspname AS schema
            FROM pg_namespace
            WHERE nspname !~ '^pg_' AND nspname <> 'information_schema'
            ORDER BY nspname
        """,
        "relations": """
            SELECT n.nspname AS schema, c.relname AS name,
                   CASE c.relkind
                     WHEN 'r' THEN 'table' WHEN 'p' THEN 'partitioned_table'
                     WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized_view'
                   END AS kind,
                   CASE WHEN c.relkind IN ('v','m')
                        THEN pg_get_viewdef(c.oid, true) ELSE NULL END AS definition
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
              AND c.relkind IN ('r','p','v','m')
            ORDER BY n.nspname, c.relname
        """,
        "columns": """
            SELECT n.nspname AS schema, c.relname AS relation,
                   a.attname AS name, a.attnum AS position,
                   pg_catalog.format_type(a.atttypid, a.atttypmod) AS type,
                   NOT a.attnotnull AS nullable,
                   CASE WHEN a.atthasdef THEN pg_get_expr(d.adbin, d.adrelid)
                        ELSE NULL END AS default,
                   NULLIF(a.attidentity, '') AS identity,
                   NULLIF(a.attgenerated, '') AS generated
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_attrdef d
              ON d.adrelid = a.attrelid AND d.adnum = a.attnum
            WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
              AND c.relkind IN ('r','p','v','m')
              AND a.attnum > 0 AND NOT a.attisdropped
            ORDER BY n.nspname, c.relname, a.attnum
        """,
        "indexes": """
            SELECT n.nspname AS schema, table_class.relname AS relation,
                   index_class.relname AS name,
                   index_row.indisunique AS unique,
                   index_row.indisprimary AS primary,
                   index_row.indisvalid AS valid,
                   (constraint_row.oid IS NOT NULL) AS constraint_backed,
                   pg_get_indexdef(index_row.indexrelid) AS definition,
                   pg_get_expr(index_row.indpred, index_row.indrelid) AS predicate
            FROM pg_index index_row
            JOIN pg_class table_class ON table_class.oid = index_row.indrelid
            JOIN pg_class index_class ON index_class.oid = index_row.indexrelid
            JOIN pg_namespace n ON n.oid = table_class.relnamespace
            LEFT JOIN pg_constraint constraint_row
              ON constraint_row.conindid = index_row.indexrelid
            WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
            ORDER BY n.nspname, table_class.relname, index_class.relname
        """,
        "unique_constraints": """
            SELECT n.nspname AS schema, c.relname AS relation,
                   con.conname AS name, pg_get_constraintdef(con.oid, true) AS definition,
                   con.convalidated AS validated
            FROM pg_constraint con
            JOIN pg_class c ON c.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
              AND con.contype IN ('p','u')
            ORDER BY n.nspname, c.relname, con.conname
        """,
        "foreign_keys": """
            SELECT n.nspname AS schema, c.relname AS relation,
                   con.conname AS name, pg_get_constraintdef(con.oid, true) AS definition,
                   con.convalidated AS validated, con.condeferrable AS deferrable,
                   con.condeferred AS initially_deferred
            FROM pg_constraint con
            JOIN pg_class c ON c.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
              AND con.contype = 'f'
            ORDER BY n.nspname, c.relname, con.conname
        """,
        "check_constraints": """
            SELECT n.nspname AS schema, c.relname AS relation,
                   con.conname AS name, pg_get_constraintdef(con.oid, true) AS definition,
                   con.convalidated AS validated
            FROM pg_constraint con
            JOIN pg_class c ON c.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
              AND con.contype = 'c'
            ORDER BY n.nspname, c.relname, con.conname
        """,
        "triggers": """
            SELECT n.nspname AS schema, c.relname AS relation,
                   t.tgname AS name, pg_get_triggerdef(t.oid, true) AS definition,
                   t.tgenabled AS enabled
            FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE NOT t.tgisinternal
              AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
            ORDER BY n.nspname, c.relname, t.tgname
        """,
        "routines": """
            SELECT n.nspname AS schema, p.proname AS name,
                   p.prokind AS kind,
                   pg_get_function_identity_arguments(p.oid) AS identity_arguments,
                   pg_get_function_result(p.oid) AS result,
                   l.lanname AS language,
                   pg_get_functiondef(p.oid) AS definition
            FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            JOIN pg_language l ON l.oid = p.prolang
            WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
            ORDER BY n.nspname, p.proname, identity_arguments
        """,
    }
    payload: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "postgresql_version": connection.execute("SHOW server_version").fetchone()[0],
    }
    if payload["postgresql_version"].split()[0] != EXPECTED_POSTGRESQL_VERSION:
        raise FingerprintMismatch(
            f"PostgreSQL version mismatch: expected {EXPECTED_POSTGRESQL_VERSION}, "
            f"observed {payload['postgresql_version']}"
        )
    for category, query in queries.items():
        payload[category] = _rows(connection, query)
    for category in (
        "relations",
        "columns",
        "indexes",
        "unique_constraints",
        "foreign_keys",
        "check_constraints",
        "triggers",
    ):
        for row in payload[category]:
            for field in ("default", "definition", "predicate"):
                if isinstance(row.get(field), str):
                    row[field] = _canonical_sql(row[field])
    for routine in payload["routines"]:
        definition = routine.pop("definition")
        routine["definition_sha256"] = hashlib.sha256(
            definition.encode("utf-8")
        ).hexdigest()
    relation_names = {
        (row["schema"], row["name"]) for row in payload["relations"]
    }
    if ("public", "alembic_version") in relation_names:
        payload["migration_revisions"] = _rows(
            connection,
            "SELECT version_num AS revision FROM public.alembic_version ORDER BY version_num",
        )
    else:
        payload["migration_revisions"] = []
    return {
        "fingerprint_sha256": payload_sha256(payload),
        "payload": payload,
    }


def _object_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(
        row.get(field)
        for field in (
            "schema",
            "relation",
            "name",
            "revision",
            "position",
            "kind",
            "identity_arguments",
        )
    )


def compare_fingerprints(
    expected: dict[str, Any],
    observed: dict[str, Any],
    *,
    allowed_extra_schemas: Iterable[str] = (),
) -> dict[str, Any]:
    """Compare application schemas exactly, permitting declared whole extra schemas."""
    expected_payload = expected["payload"]
    observed_payload = observed["payload"]
    allowed = set(allowed_extra_schemas)
    differences: list[dict[str, Any]] = []
    for category in CATEGORIES:
        wanted_rows = expected_payload.get(category, [])
        actual_rows = [
            row
            for row in observed_payload.get(category, [])
            if row.get("schema") not in allowed
        ]
        if wanted_rows != actual_rows:
            wanted = {_object_key(row): row for row in wanted_rows}
            actual = {_object_key(row): row for row in actual_rows}
            differences.append(
                {
                    "category": category,
                    "missing": [wanted[key] for key in sorted(set(wanted) - set(actual))],
                    "unexpected": [actual[key] for key in sorted(set(actual) - set(wanted))],
                    "changed": [
                        {"expected": wanted[key], "observed": actual[key]}
                        for key in sorted(set(wanted) & set(actual))
                        if wanted[key] != actual[key]
                    ],
                }
            )
    return {
        "compatible": not differences,
        "expected_sha256": expected["fingerprint_sha256"],
        "observed_sha256": observed["fingerprint_sha256"],
        "allowed_extra_schemas": sorted(allowed),
        "differences": differences,
    }


def require_fingerprint_match(
    expected: dict[str, Any],
    observed: dict[str, Any],
    *,
    allowed_extra_schemas: Iterable[str] = (),
) -> dict[str, Any]:
    result = compare_fingerprints(
        expected, observed, allowed_extra_schemas=allowed_extra_schemas
    )
    if not result["compatible"]:
        categories = ", ".join(item["category"] for item in result["differences"])
        raise FingerprintMismatch(
            "schema object mismatch despite migration revision: " + categories
        )
    return result


def write_fingerprint(fingerprint: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(fingerprint) + b"\n")


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url-env", default="DATABASE_URL")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> int:
    args = parse_args(arguments)
    database_url = os.getenv(args.database_url_env)
    if not database_url:
        raise SystemExit(f"{args.database_url_env} is required")
    database_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        with psycopg.connect(database_url) as connection:
            fingerprint = fingerprint_connection(connection)
    except (psycopg.Error, FingerprintMismatch) as error:
        print(json.dumps({"status": "FAIL", "error": str(error)}, ensure_ascii=False))
        return 1
    write_fingerprint(fingerprint, args.output)
    print(
        json.dumps(
            {
                "status": "PASS",
                "fingerprint_sha256": fingerprint["fingerprint_sha256"],
                "migration_revisions": fingerprint["payload"]["migration_revisions"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

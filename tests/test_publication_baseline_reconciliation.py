from __future__ import annotations

from dataclasses import replace
import sqlite3
from typing import Any

import pytest

from public_app.contracts import HASH_ALGORITHM_VERSION, PROJECTION_VERSION
from scripts.public_release_common import semantic_projection_sha256
from scripts.reconcile_publication_baseline import (
    OperationalPublication,
    PsycopgOperationalStore,
    ReconciliationBlocked,
    ReconciliationExpectations,
    build_reconciliation_plan,
    parse_args,
    reconcile_publication_baseline,
)
from tests.public_test_support import forty_projections, projection


RELEASE_ID = "public-v1-20260927T040214Z-32334077-8f44d69a"
ALAN_INN = "0100000614"
RETAINED_CONTROL_INN = "0100000639"


def _parent_projections():
    records = forty_projections(RELEASE_ID)
    alan = projection(sequence=910_000_000, release_id=RELEASE_ID)
    alan = alan.model_copy(
        update={
            "company": alan.company.model_copy(
                update={"inn": ALAN_INN, "name": "ООО «АЛАН»"}
            )
        }
    )
    control = projection(sequence=910_000_001, release_id=RELEASE_ID)
    control = control.model_copy(
        update={
            "company": control.company.model_copy(
                update={"inn": RETAINED_CONTROL_INN, "name": "ООО «КОНТРОЛЬ»"}
            )
        }
    )
    records[-2:] = [control, alan]
    return sorted(records, key=lambda item: item.company.inn)


def _operational_rows(*, mismatch: bool = True):
    rows = []
    for company_id, item in enumerate(_parent_projections(), 1):
        inn = item.company.inn
        rows.append(
            OperationalPublication(
                company_id=company_id,
                inn=inn,
                last_published_hash=(
                    "f" * 64
                    if mismatch and inn != ALAN_INN
                    else "a" * 64
                    if inn == ALAN_INN
                    else semantic_projection_sha256(item)
                ),
                projection_version=(
                    "public-projection-v1.legacy"
                    if inn == ALAN_INN
                    else PROJECTION_VERSION
                ),
                hash_algorithm_version=(
                    "sha256-canonical-json-v1"
                    if inn == ALAN_INN
                    else HASH_ALGORITHM_VERSION
                ),
                is_published=True,
                last_published_release_id=RELEASE_ID,
                last_enrichment_run_id=None,
                published_at="preserved-published-at",
                updated_at="preserved-updated-at",
            )
        )
    return rows


class MemoryStore:
    def __init__(self, rows):
        self.rows = {row.inn: row for row in rows}
        self.lock_reads: list[bool] = []
        self.updated_inns: list[str] = []
        self.publication_lock_count = 0

    def acquire_publication_lock(self):
        self.publication_lock_count += 1

    def read_published_parent(self, *, lock: bool):
        self.lock_reads.append(lock)
        return [self.rows[inn] for inn in sorted(self.rows)]

    def update_hash(self, item, row):
        current = self.rows.get(item.inn)
        if current != row:
            return 0
        self.rows[item.inn] = replace(
            current, last_published_hash=item.trusted_active_semantic_hash
        )
        self.updated_inns.append(item.inn)
        return 1


class SqliteTestStore:
    """Small transactional test DB for the exact hash-only apply contract."""

    def __init__(self, rows):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """
            CREATE TABLE publications (
                company_id INTEGER PRIMARY KEY,
                inn TEXT NOT NULL UNIQUE,
                last_published_hash TEXT NOT NULL,
                projection_version TEXT NOT NULL,
                hash_algorithm_version TEXT NOT NULL,
                is_published INTEGER NOT NULL,
                last_published_release_id TEXT NOT NULL,
                last_enrichment_run_id TEXT,
                published_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.connection.executemany(
            "INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    row.company_id,
                    row.inn,
                    row.last_published_hash,
                    row.projection_version,
                    row.hash_algorithm_version,
                    int(row.is_published),
                    row.last_published_release_id,
                    row.last_enrichment_run_id,
                    row.published_at,
                    row.updated_at,
                )
                for row in rows
            ],
        )
        self.publication_lock_count = 0
        self.lock_reads: list[bool] = []
        self.updated_inns: list[str] = []

    @property
    def rows(self):
        return {
            row.inn: row for row in self.read_published_parent(lock=False)
        }

    def acquire_publication_lock(self):
        self.publication_lock_count += 1

    def read_published_parent(self, *, lock: bool):
        self.lock_reads.append(lock)
        records = self.connection.execute(
            "SELECT * FROM publications WHERE is_published = 1 ORDER BY inn"
        ).fetchall()
        return [
            OperationalPublication(
                **{
                    **dict(record),
                    "is_published": bool(record["is_published"]),
                }
            )
            for record in records
        ]

    def update_hash(self, item, row):
        cursor = self.connection.execute(
            """
            UPDATE publications
               SET last_published_hash = ?
             WHERE company_id = ?
               AND last_published_hash = ?
               AND projection_version = ?
               AND hash_algorithm_version = ?
               AND is_published = 1
               AND last_published_release_id = ?
            """,
            (
                item.trusted_active_semantic_hash,
                row.company_id,
                item.operational_last_published_hash,
                row.projection_version,
                row.hash_algorithm_version,
                row.last_published_release_id,
            ),
        )
        if cursor.rowcount == 1:
            self.updated_inns.append(item.inn)
        return cursor.rowcount


class TrustedReader:
    def __init__(self, projections=None):
        self.projections = projections or _parent_projections()
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.release_id = RELEASE_ID
        self.record_count = 40
        self.member_inns = tuple(item.company.inn for item in self.projections)
        self.payload_overrides: dict[str, Any] = {}
        self.omit: set[str] = set()
        self.mutation_count = 0

    def __call__(self, release_id, inns):
        self.calls.append((release_id, inns))
        by_inn = {item.company.inn: item for item in self.projections}
        return {
            "release_id": self.release_id,
            "record_count": self.record_count,
            "member_inns": self.member_inns,
            "projections": [
                {
                    "inn": inn,
                    "payload": self.payload_overrides.get(
                        inn, by_inn[inn].model_dump(mode="json")
                    ),
                }
                for inn in inns
                if inn not in self.omit
            ],
        }


def _expectations(mismatch_count=39):
    return ReconciliationExpectations(
        release_id=RELEASE_ID,
        parent_count=40,
        retained_count=39,
        withdrawn_inn=ALAN_INN,
        retained_control_inn=RETAINED_CONTROL_INN,
        expected_mismatch_count=mismatch_count,
    )


def test_39_mismatches_produce_a_deterministic_plan_without_parsing_alan():
    reader = TrustedReader()
    first = build_reconciliation_plan(
        _operational_rows(), reader, _expectations()
    )
    second = build_reconciliation_plan(
        list(reversed(_operational_rows())), reader, _expectations()
    )

    assert first.mismatch_count == 39
    assert first.plan_sha256 == second.plan_sha256
    assert first.deterministic_payload() == second.deterministic_payload()
    assert all(item.action == "UPDATE_HASH" for item in first.items)
    assert ALAN_INN not in {item.inn for item in first.items}
    assert all(ALAN_INN not in inns for _release, inns in reader.calls)


def test_apply_updates_exactly_39_hashes_and_second_apply_is_no_op():
    store = SqliteTestStore(_operational_rows())
    reader = TrustedReader()
    alan_before = store.rows[ALAN_INN]

    applied = reconcile_publication_baseline(
        store, reader, _expectations(), apply=True
    )
    repeated = reconcile_publication_baseline(
        store, reader, _expectations(mismatch_count=0), apply=True
    )

    assert applied["result"] == "APPLIED"
    assert applied["update_count"] == 39
    assert applied["post_apply_mismatch_count"] == 0
    assert repeated["result"] == "NO_OP"
    assert repeated["update_count"] == 0
    assert store.updated_inns == sorted(
        inn for inn in store.rows if inn != ALAN_INN
    )
    assert store.rows[ALAN_INN] == alan_before
    assert reader.mutation_count == 0
    assert store.lock_reads[1:5] == [True, True, True, True]
    assert store.publication_lock_count == 2


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda _rows, reader: setattr(reader, "release_id", "public-v1-wrong"), "release mismatch"),
        (lambda _rows, reader: setattr(reader, "record_count", 39), "count mismatch"),
        (lambda rows, _reader: rows.pop(), "operational published parent count mismatch"),
        (
            lambda _rows, reader: setattr(
                reader,
                "member_inns",
                tuple(
                    sorted(
                        (*reader.member_inns[:-1], projection(sequence=910_000_002, release_id=RELEASE_ID).company.inn)
                    )
                ),
            ),
            "parent membership mismatch",
        ),
        (
            lambda _rows, reader: reader.omit.add(
                next(inn for inn in reader.member_inns if inn != ALAN_INN)
            ),
            "projection membership mismatch",
        ),
    ],
)
def test_identity_count_membership_and_missing_projection_fail_closed(mutate, match):
    rows = _operational_rows()
    reader = TrustedReader()
    mutate(rows, reader)

    with pytest.raises(ReconciliationBlocked, match=match):
        build_reconciliation_plan(rows, reader, _expectations())


def test_invalid_trusted_projection_fails_closed():
    reader = TrustedReader()
    inn = next(inn for inn in reader.member_inns if inn != ALAN_INN)
    invalid = next(
        item.model_dump(mode="json")
        for item in reader.projections
        if item.company.inn == inn
    )
    invalid.pop("company")
    reader.payload_overrides[inn] = invalid

    with pytest.raises(ReconciliationBlocked, match="projection is invalid"):
        build_reconciliation_plan(_operational_rows(), reader, _expectations())


def test_retained_version_metadata_ambiguity_fails_closed_without_updates():
    rows = _operational_rows()
    retained_index = next(
        index for index, row in enumerate(rows) if row.inn != ALAN_INN
    )
    rows[retained_index] = replace(
        rows[retained_index], hash_algorithm_version="sha256-unknown"
    )
    store = MemoryStore(rows)

    with pytest.raises(ReconciliationBlocked, match="version metadata is ambiguous"):
        reconcile_publication_baseline(
            store, TrustedReader(), _expectations(), apply=True
        )

    assert store.updated_inns == []


def test_dry_run_never_locks_or_updates():
    store = MemoryStore(_operational_rows())
    result = reconcile_publication_baseline(
        store, TrustedReader(), _expectations(), apply=False
    )

    assert result["result"] == "PLAN_READY"
    assert result["mismatch_count"] == 39
    assert result["update_count"] == 0
    assert result["alan_mutation"] == 0
    assert result["public_database_mutation"] == 0
    assert store.lock_reads == [False]
    assert store.updated_inns == []
    assert store.publication_lock_count == 0


class _CaptureCursor:
    def __init__(self):
        self.statement = ""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, statement, _parameters=None):
        self.statement = statement

    def fetchall(self):
        return []


class _CaptureConnection:
    def __init__(self):
        self.cursor_instance = _CaptureCursor()
        self.executed = []

    def cursor(self, **_kwargs):
        return self.cursor_instance

    def execute(self, statement, parameters=None):
        self.executed.append((statement, parameters))


def test_apply_store_uses_transactional_row_lock():
    connection = _CaptureConnection()
    store = PsycopgOperationalStore(connection)
    store.acquire_publication_lock()
    store.read_published_parent(lock=True)
    assert connection.executed == [
        ("SELECT pg_advisory_xact_lock(%s)", (0x4E435053,))
    ]
    assert "FOR UPDATE OF p" in connection.cursor_instance.statement


def test_apply_cli_requires_explicit_mismatch_and_release_confirmation():
    common = [
        "apply",
        "--operational-database-url",
        "postgresql://home/test",
        "--expected-release-id",
        RELEASE_ID,
        "--expected-parent-count",
        "40",
        "--expected-retained-count",
        "39",
        "--expected-withdrawn-inn",
        ALAN_INN,
        "--expected-retained-control-inn",
        RETAINED_CONTROL_INN,
    ]
    with pytest.raises(SystemExit):
        parse_args(common)
    with pytest.raises(SystemExit):
        parse_args([*common, "--expected-mismatch-count", "39"])

    args = parse_args(
        [
            *common,
            "--expected-mismatch-count",
            "39",
            "--confirm-apply",
            RELEASE_ID,
        ]
    )
    assert args.action == "apply"

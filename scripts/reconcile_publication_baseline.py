#!/usr/bin/env python3
"""Reconcile HOME publication hashes with one exact trusted active release.

The default ``dry-run`` action is read-only.  ``apply`` requires an explicit
mismatch count and release confirmation, locks the operational parent rows,
and updates only proven retained ``last_published_hash`` values.  The public
database is always opened through the existing read-only trusted reader.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import os
import re
import sys
from collections.abc import Callable, Sequence
from typing import Any, Protocol

import psycopg
from pydantic import ValidationError
from psycopg.rows import dict_row

from public_app.contracts import (
    HASH_ALGORITHM_VERSION,
    PROJECTION_VERSION,
    PublicProjection,
    valid_legal_inn,
)
from scripts.public_release_common import canonical_json, semantic_projection_sha256
from scripts.read_public_release import SAFE_RELEASE


TASK_ID = "PUBLIC-SYNC-PUBLISHED-BASELINE-RECONCILIATION-01"
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PUBLIC_SYNC_LOCK_ID = 0x4E435053  # Must match publication_service.PUBLIC_SYNC_LOCK_ID.


class ReconciliationBlocked(RuntimeError):
    """An exact expectation or preservation invariant was not satisfied."""


@dataclass(frozen=True)
class ReconciliationExpectations:
    release_id: str
    parent_count: int
    retained_count: int
    withdrawn_inn: str
    retained_control_inn: str
    expected_mismatch_count: int | None = None

    def validate(self, *, apply: bool) -> None:
        if not SAFE_RELEASE.fullmatch(self.release_id):
            raise ReconciliationBlocked("expected release ID is invalid")
        if self.parent_count < 1:
            raise ReconciliationBlocked("expected parent count must be positive")
        if self.retained_count != self.parent_count - 1:
            raise ReconciliationBlocked(
                "expected retained count must equal parent count minus one withdrawal"
            )
        if not valid_legal_inn(self.withdrawn_inn):
            raise ReconciliationBlocked("expected withdrawn INN is invalid")
        if (
            not valid_legal_inn(self.retained_control_inn)
            or self.retained_control_inn == self.withdrawn_inn
        ):
            raise ReconciliationBlocked("expected retained control INN is invalid")
        if self.expected_mismatch_count is not None and not (
            0 <= self.expected_mismatch_count <= self.retained_count
        ):
            raise ReconciliationBlocked("expected mismatch count is invalid")
        if apply and self.expected_mismatch_count is None:
            raise ReconciliationBlocked("apply requires expected mismatch count")


@dataclass(frozen=True)
class OperationalPublication:
    company_id: int
    inn: str
    last_published_hash: str
    projection_version: str
    hash_algorithm_version: str
    is_published: bool
    last_published_release_id: str
    last_enrichment_run_id: Any
    published_at: Any
    updated_at: Any


@dataclass(frozen=True)
class ReconciliationItem:
    company_id: int
    inn: str
    operational_last_published_hash: str
    trusted_active_semantic_hash: str
    action: str


@dataclass(frozen=True)
class ReconciliationPlan:
    release_id: str
    parent_count: int
    retained_count: int
    withdrawn_inn: str
    retained_control_inn: str
    mismatch_count: int
    items: tuple[ReconciliationItem, ...]

    def deterministic_payload(self) -> dict[str, Any]:
        return {
            "release_id": self.release_id,
            "parent_count": self.parent_count,
            "retained_count": self.retained_count,
            "withdrawn_inn": self.withdrawn_inn,
            "retained_control_inn": self.retained_control_inn,
            "mismatch_count": self.mismatch_count,
            "items": [asdict(item) for item in self.items],
        }

    @property
    def plan_sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.deterministic_payload())).hexdigest()


class OperationalStore(Protocol):
    def acquire_publication_lock(self) -> None: ...

    def read_published_parent(self, *, lock: bool) -> list[OperationalPublication]: ...

    def update_hash(self, item: ReconciliationItem, row: OperationalPublication) -> int: ...


TrustedReader = Callable[[str, tuple[str, ...]], dict[str, Any]]


OPERATIONAL_PARENT_SQL = """
    SELECT p.company_id, c.inn, p.last_published_hash,
           p.projection_version, p.hash_algorithm_version,
           p.is_published, p.last_published_release_id,
           p.last_enrichment_run_id, p.published_at, p.updated_at
      FROM public_projection_publications p
      JOIN companies c ON c.id = p.company_id
     WHERE p.is_published = TRUE
     ORDER BY c.inn
"""


class PsycopgOperationalStore:
    """Narrow operational persistence boundary used by the controlled command."""

    def __init__(self, connection) -> None:
        self.connection = connection

    def acquire_publication_lock(self) -> None:
        self.connection.execute(
            "SELECT pg_advisory_xact_lock(%s)", (PUBLIC_SYNC_LOCK_ID,)
        )

    def read_published_parent(self, *, lock: bool) -> list[OperationalPublication]:
        statement = OPERATIONAL_PARENT_SQL
        if lock:
            statement += " FOR UPDATE OF p"
        with self.connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(statement)
            return [OperationalPublication(**dict(row)) for row in cursor.fetchall()]

    def update_hash(self, item: ReconciliationItem, row: OperationalPublication) -> int:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE public_projection_publications
                   SET last_published_hash = %s
                 WHERE company_id = %s
                   AND last_published_hash = %s
                   AND projection_version = %s
                   AND hash_algorithm_version = %s
                   AND is_published = TRUE
                   AND last_published_release_id = %s
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
            return cursor.rowcount


def _normalize_operational_rows(
    rows: Sequence[OperationalPublication],
    expectations: ReconciliationExpectations,
) -> tuple[OperationalPublication, ...]:
    normalized = tuple(sorted(rows, key=lambda row: row.inn))
    inns = tuple(row.inn for row in normalized)
    if len(normalized) != expectations.parent_count:
        raise ReconciliationBlocked(
            "operational published parent count mismatch: "
            f"expected={expectations.parent_count} actual={len(normalized)}"
        )
    if len(set(inns)) != len(inns) or any(not valid_legal_inn(inn) for inn in inns):
        raise ReconciliationBlocked("operational parent membership is invalid")
    if expectations.withdrawn_inn not in inns:
        raise ReconciliationBlocked("expected withdrawn INN is not a parent member")
    if expectations.retained_control_inn not in inns:
        raise ReconciliationBlocked("expected retained control INN is not a parent member")
    for row in normalized:
        if not row.is_published:
            raise ReconciliationBlocked("operational parent contains an unpublished row")
        if row.last_published_release_id != expectations.release_id:
            raise ReconciliationBlocked(
                f"operational parent release mismatch: {row.inn}"
            )
        if not HASH_PATTERN.fullmatch(row.last_published_hash):
            raise ReconciliationBlocked(
                f"operational parent hash is invalid: {row.inn}"
            )
    return normalized


def _trusted_projection_map(
    trusted: dict[str, Any],
    *,
    retained_inns: tuple[str, ...],
    expectations: ReconciliationExpectations,
) -> dict[str, PublicProjection]:
    if trusted.get("release_id") != expectations.release_id:
        raise ReconciliationBlocked("trusted active release mismatch")
    try:
        record_count = int(trusted.get("record_count"))
    except (TypeError, ValueError) as error:
        raise ReconciliationBlocked("trusted active release count is invalid") from error
    if record_count != expectations.parent_count:
        raise ReconciliationBlocked(
            "trusted active release count mismatch: "
            f"expected={expectations.parent_count} actual={record_count}"
        )
    member_inns = tuple(trusted.get("member_inns") or ())
    if (
        tuple(sorted(member_inns)) != member_inns
        or len(member_inns) != expectations.parent_count
        or len(set(member_inns)) != len(member_inns)
        or any(not valid_legal_inn(inn) for inn in member_inns)
    ):
        raise ReconciliationBlocked("trusted active release membership is invalid")

    raw_projections = trusted.get("projections")
    if not isinstance(raw_projections, list):
        raise ReconciliationBlocked("trusted active projections response is invalid")
    payloads: dict[str, Any] = {}
    for entry in raw_projections:
        if not isinstance(entry, dict) or set(entry) != {"inn", "payload"}:
            raise ReconciliationBlocked("trusted active projection entry is invalid")
        inn = entry["inn"]
        if inn in payloads:
            raise ReconciliationBlocked("trusted active projections contain duplicates")
        payloads[inn] = entry["payload"]
    if tuple(sorted(payloads)) != retained_inns:
        raise ReconciliationBlocked("trusted retained projection membership mismatch")

    result: dict[str, PublicProjection] = {}
    for inn in retained_inns:
        try:
            projection = PublicProjection.model_validate(payloads[inn])
        except (ValidationError, TypeError, ValueError) as error:
            raise ReconciliationBlocked(
                f"trusted retained projection is invalid: {inn}"
            ) from error
        if projection.company.inn != inn:
            raise ReconciliationBlocked(
                f"trusted retained projection INN mismatch: {inn}"
            )
        if projection.publication.release_id != expectations.release_id:
            raise ReconciliationBlocked(
                f"trusted retained projection release mismatch: {inn}"
            )
        result[inn] = projection
    return result


def build_reconciliation_plan(
    rows: Sequence[OperationalPublication],
    trusted_reader: TrustedReader,
    expectations: ReconciliationExpectations,
    *,
    enforce_expected_mismatch: bool = True,
) -> ReconciliationPlan:
    """Build a deterministic plan without mutating either persistence boundary."""

    expectations.validate(apply=False)
    parent = _normalize_operational_rows(rows, expectations)
    parent_by_inn = {row.inn: row for row in parent}
    retained_inns = tuple(
        inn for inn in sorted(parent_by_inn) if inn != expectations.withdrawn_inn
    )
    if len(retained_inns) != expectations.retained_count:
        raise ReconciliationBlocked(
            "retained count mismatch: "
            f"expected={expectations.retained_count} actual={len(retained_inns)}"
        )

    for inn in retained_inns:
        row = parent_by_inn[inn]
        if (
            row.projection_version != PROJECTION_VERSION
            or row.hash_algorithm_version != HASH_ALGORITHM_VERSION
        ):
            raise ReconciliationBlocked(
                f"retained version metadata is ambiguous: {inn}"
            )

    try:
        trusted = trusted_reader(expectations.release_id, retained_inns)
    except ReconciliationBlocked:
        raise
    except Exception as error:
        raise ReconciliationBlocked(
            f"trusted active release read failed: {type(error).__name__}"
        ) from error
    projections = _trusted_projection_map(
        trusted,
        retained_inns=retained_inns,
        expectations=expectations,
    )
    if tuple(trusted["member_inns"]) != tuple(sorted(parent_by_inn)):
        raise ReconciliationBlocked(
            "trusted and operational parent membership mismatch"
        )

    planned_items: list[ReconciliationItem] = []
    for inn in retained_inns:
        row = parent_by_inn[inn]
        trusted_hash = semantic_projection_sha256(projections[inn])
        planned_items.append(
            ReconciliationItem(
                company_id=row.company_id,
                inn=inn,
                operational_last_published_hash=row.last_published_hash,
                trusted_active_semantic_hash=trusted_hash,
                action=(
                    "NO_OP"
                    if row.last_published_hash == trusted_hash
                    else "UPDATE_HASH"
                ),
            )
        )
    items = tuple(planned_items)
    mismatch_count = sum(item.action == "UPDATE_HASH" for item in items)
    if (
        enforce_expected_mismatch
        and expectations.expected_mismatch_count is not None
        and mismatch_count != expectations.expected_mismatch_count
    ):
        raise ReconciliationBlocked(
            "mismatch count differs from explicit expectation: "
            f"expected={expectations.expected_mismatch_count} actual={mismatch_count}"
        )
    return ReconciliationPlan(
        release_id=expectations.release_id,
        parent_count=expectations.parent_count,
        retained_count=expectations.retained_count,
        withdrawn_inn=expectations.withdrawn_inn,
        retained_control_inn=expectations.retained_control_inn,
        mismatch_count=mismatch_count,
        items=items,
    )


def reconcile_publication_baseline(
    store: OperationalStore,
    trusted_reader: TrustedReader,
    expectations: ReconciliationExpectations,
    *,
    apply: bool,
) -> dict[str, Any]:
    """Plan or apply the hash-only repair inside the caller's transaction."""

    expectations.validate(apply=apply)
    if apply:
        store.acquire_publication_lock()
    before = tuple(store.read_published_parent(lock=apply))
    before_by_inn = {row.inn: row for row in before}
    plan = build_reconciliation_plan(before, trusted_reader, expectations)
    updates = tuple(item for item in plan.items if item.action == "UPDATE_HASH")

    if not apply:
        return {
            "task": TASK_ID,
            "mode": "DRY_RUN",
            "result": "PLAN_READY" if updates else "NO_OP",
            **plan.deterministic_payload(),
            "update_count": 0,
            "alan_mutation": 0,
            "public_database_mutation": 0,
            "plan_sha256": plan.plan_sha256,
        }

    for item in updates:
        if store.update_hash(item, before_by_inn[item.inn]) != 1:
            raise ReconciliationBlocked(
                f"compare-and-swap update failed: {item.inn}"
            )

    after = tuple(store.read_published_parent(lock=True))
    after_by_inn = {row.inn: row for row in after}
    post_plan = build_reconciliation_plan(
        after,
        trusted_reader,
        replace(expectations, expected_mismatch_count=None),
        enforce_expected_mismatch=False,
    )
    if post_plan.mismatch_count != 0:
        raise ReconciliationBlocked("post-apply retained hash verification failed")
    if after_by_inn.get(expectations.withdrawn_inn) != before_by_inn.get(
        expectations.withdrawn_inn
    ):
        raise ReconciliationBlocked("withdrawn Alan row changed")
    for item in plan.items:
        previous = before_by_inn[item.inn]
        current = after_by_inn.get(item.inn)
        if current is None or replace(current, last_published_hash=previous.last_published_hash) != previous:
            raise ReconciliationBlocked(
                f"non-hash retained publication state changed: {item.inn}"
            )

    return {
        "task": TASK_ID,
        "mode": "APPLY",
        "result": "APPLIED" if updates else "NO_OP",
        **plan.deterministic_payload(),
        "update_count": len(updates),
        "post_apply_mismatch_count": post_plan.mismatch_count,
        "alan_mutation": 0,
        "release_id_mutation": 0,
        "is_published_mutation": 0,
        "public_database_mutation": 0,
        "plan_sha256": plan.plan_sha256,
    }


def _libpq_url(value: str) -> str:
    return value.replace("postgresql+psycopg://", "postgresql://", 1)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("dry-run", "apply"))
    parser.add_argument(
        "--operational-database-url",
        default=os.getenv("DATABASE_URL"),
        help="HOME operational database; defaults to DATABASE_URL",
    )
    parser.add_argument("--expected-release-id", required=True)
    parser.add_argument("--expected-parent-count", required=True, type=int)
    parser.add_argument("--expected-retained-count", required=True, type=int)
    parser.add_argument("--expected-withdrawn-inn", required=True)
    parser.add_argument("--expected-retained-control-inn", required=True)
    parser.add_argument("--expected-mismatch-count", type=int)
    parser.add_argument(
        "--confirm-apply",
        help="apply only: must exactly repeat --expected-release-id",
    )
    args = parser.parse_args(argv)
    if not args.operational_database_url:
        parser.error("DATABASE_URL or --operational-database-url is required")
    if args.action == "apply":
        if args.expected_mismatch_count is None:
            parser.error("apply requires --expected-mismatch-count")
        if args.confirm_apply != args.expected_release_id:
            parser.error("apply requires --confirm-apply equal to the expected release ID")
    elif args.confirm_apply is not None:
        parser.error("--confirm-apply is valid only for apply")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    expectations = ReconciliationExpectations(
        release_id=args.expected_release_id,
        parent_count=args.expected_parent_count,
        retained_count=args.expected_retained_count,
        withdrawn_inn=args.expected_withdrawn_inn,
        retained_control_inn=args.expected_retained_control_inn,
        expected_mismatch_count=args.expected_mismatch_count,
    )

    try:
        # Import only after parsing so the explicit operational URL can supply
        # DATABASE_URL to the existing HOME transport module.  The transport's
        # read method is the host-key-pinned, importer-authorized, read-only
        # boundary already used by public sync; no direct public DB credential is
        # accepted by this command.
        os.environ.setdefault("DATABASE_URL", args.operational_database_url)
        from scripts.run_public_sync import SshPublicTransport

        transport = SshPublicTransport()

        def trusted_reader(release_id: str, inns: tuple[str, ...]) -> dict[str, Any]:
            batch = transport.read_active_projections(release_id, inns)
            return {
                "release_id": batch.release_id,
                "record_count": batch.record_count,
                "member_inns": batch.member_inns,
                "projections": [
                    {"inn": inn, "payload": batch.projections[inn]}
                    for inn in sorted(batch.projections)
                ],
            }

        with psycopg.connect(
            _libpq_url(args.operational_database_url), row_factory=dict_row
        ) as connection:
            if args.action == "dry-run":
                connection.execute("SET TRANSACTION READ ONLY")
            result = reconcile_publication_baseline(
                PsycopgOperationalStore(connection),
                trusted_reader,
                expectations,
                apply=args.action == "apply",
            )
    except ReconciliationBlocked as error:
        print(
            json.dumps(
                {
                    "task": TASK_ID,
                    "status": "BLOCKED",
                    "error": str(error),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2
    except Exception as error:
        print(
            json.dumps(
                {
                    "task": TASK_ID,
                    "status": "FAIL",
                    "error_type": type(error).__name__,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

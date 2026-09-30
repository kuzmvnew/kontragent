#!/usr/bin/env python3
"""Read full projections for one exact active release over a trusted boundary."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row


SAFE_RELEASE = re.compile(r"^[a-zA-Z0-9._-]{8,120}$")
LEGAL_INN = re.compile(r"^[0-9]{10}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _payload_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def read_active_release_projections(
    connection,
    *,
    expected_release_id: str,
    inns: Iterable[str],
) -> dict[str, Any]:
    """Return full stored payloads only when the exact release is still active."""

    if not SAFE_RELEASE.fullmatch(expected_release_id):
        raise ValueError("invalid expected release ID")
    requested = tuple(inns)
    if (
        len(requested) != len(set(requested))
        or tuple(sorted(requested)) != requested
        or any(not LEGAL_INN.fullmatch(inn) for inn in requested)
    ):
        raise ValueError("requested INNs must be unique, sorted legal-entity identifiers")

    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute(
            """
            SELECT r.release_id, r.record_count, r.status,
                   (SELECT count(*)
                      FROM public_company_projections p
                     WHERE p.release_id = r.release_id) AS actual_record_count
              FROM public_publication_state s
              JOIN public_releases r ON r.release_id = s.active_release_id
             WHERE s.singleton = TRUE
            """
        )
        release = cursor.fetchone()
        if release is None:
            raise ValueError("active public release is missing")
        if release["release_id"] != expected_release_id:
            raise ValueError("active public release does not match expected release")
        if release["status"] != "active":
            raise ValueError("active public release metadata is inconsistent")
        record_count = int(release["record_count"])
        if int(release["actual_record_count"]) != record_count:
            raise ValueError("active public release record count is inconsistent")

        cursor.execute(
            """
            SELECT inn
              FROM public_company_projections
             WHERE release_id = %s
             ORDER BY inn
            """,
            (expected_release_id,),
        )
        member_inns = tuple(row["inn"] for row in cursor.fetchall())
        if len(member_inns) != record_count or len(set(member_inns)) != record_count:
            raise ValueError("active public release membership is inconsistent")

        rows: list[dict[str, Any]] = []
        if requested:
            cursor.execute(
                """
                SELECT inn, payload, payload_sha256
                  FROM public_company_projections
                 WHERE release_id = %s AND inn = ANY(%s)
                 ORDER BY inn
                """,
                (expected_release_id, list(requested)),
            )
            rows = list(cursor.fetchall())
        observed = tuple(row["inn"] for row in rows)
        if observed != requested:
            raise ValueError("trusted active release projection membership mismatch")
        for row in rows:
            stored_sha256 = str(row["payload_sha256"])
            if (
                not SHA256.fullmatch(stored_sha256)
                or _payload_sha256(row["payload"]) != stored_sha256
            ):
                raise ValueError("trusted active release payload integrity mismatch")

    return {
        "release_id": expected_release_id,
        "record_count": record_count,
        "member_inns": member_inns,
        "projections": [
            {
                "inn": row["inn"],
                "payload": row["payload"],
                "payload_sha256": row["payload_sha256"],
            }
            for row in rows
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", default=os.getenv("PUBLIC_IMPORT_DATABASE_URL"))
    parser.add_argument("--expected-release-id", required=True)
    parser.add_argument("--inn", action="append", default=[])
    args = parser.parse_args()
    if not args.database_url:
        parser.error("PUBLIC_IMPORT_DATABASE_URL or --database-url is required")
    url = args.database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        with psycopg.connect(url) as connection:
            result = read_active_release_projections(
                connection,
                expected_release_id=args.expected_release_id,
                inns=args.inn,
            )
    except Exception as error:
        # This command crosses an operational boundary. Never serialize rejected
        # payloads, SQL details, or connection configuration into its diagnostics.
        print(
            f"trusted public release read failed: {type(error).__name__}",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

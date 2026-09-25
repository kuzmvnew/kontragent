#!/usr/bin/env python3
"""Select and persist the canonical public cohort from operational Master."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.contracts.risk_v3 import UnsupportedSubjectOutcome
from app.services.risk_v3_persistence_service import (
    calculate_company_risk_v3_from_persisted,
    get_or_create_summary_v3,
)
from public_app.contracts import (
    SCHEMA_VERSION,
    CanonicalManifest,
    ManifestEntity,
    PublicationInfo,
    valid_legal_inn,
)
from scripts.export_public_release import build_projection

POLICY_VERSION = "public-cohort-selection-v1"
SOURCE_TABLES = {
    "DEBTAM": "company_tax_debt_snapshots",
    "HEADCOUNT": "company_headcounts",
    "MSP": "company_msp_profiles",
    "PAYTAX": "company_tax_payment_snapshots",
    "REVEXP": "company_revenue_expense_snapshots",
    "TAXOFFENCE": "company_tax_offences",
}


def _coverage(cursor, company_id: int) -> tuple[str, ...]:
    covered = []
    for code, table in SOURCE_TABLES.items():
        cursor.execute(
            f"SELECT EXISTS(SELECT 1 FROM {table} WHERE company_id=%s) AS value",
            (company_id,),
        )
        if cursor.fetchone()["value"]:
            covered.append(code)
    return tuple(sorted(covered))


def rank_candidates(candidates: list[dict]) -> list[dict]:
    """Rank by usable persisted source coverage, never by risk outcome."""

    return sorted(
        candidates,
        key=lambda item: (-len(item["usable_source_coverage"]), item["inn"]),
    )


def select_candidates(cursor) -> list[dict]:
    cursor.execute(
        """SELECT c.id, c.inn, c.ogrn, c.source, ds.code AS master_dataset
           FROM companies c
           JOIN data_sets ds ON ds.id=c.master_dataset_id
           WHERE c.entity_type='legal'
             AND c.source='fns'
             AND ds.code='fns_egrul'
           ORDER BY c.inn"""
    )
    rows = cursor.fetchall()
    eligible = []
    for row in rows:
        inn = str(row["inn"])
        ogrn = str(row["ogrn"] or "")
        if not valid_legal_inn(inn) or not re.fullmatch(r"\d{13}", ogrn):
            continue
        eligible.append(
            {
                "id": int(row["id"]),
                "inn": inn,
                "entity_type": "legal",
                "master_dataset": row["master_dataset"],
                "source": row["source"],
                "usable_source_coverage": _coverage(cursor, int(row["id"])),
            }
        )
    ranked = rank_candidates(eligible)
    if len(ranked) < 40:
        raise ValueError(f"only {len(ranked)} official legal entities are eligible")
    return sorted(ranked[:40], key=lambda item: item["inn"])


def persist_risk_summary(
    database_url: str,
    selected: list[dict],
    *,
    calculated_at: datetime,
) -> dict[str, int]:
    engine = create_engine(database_url, pool_pre_ping=True)
    counters = {"risk_created": 0, "risk_reused": 0, "summary_created": 0, "summary_reused": 0}
    try:
        with Session(engine) as session, session.begin():
            for item in selected:
                risk, risk_reused = calculate_company_risk_v3_from_persisted(
                    session,
                    int(item["id"]),
                    calculated_at=calculated_at,
                )
                if isinstance(risk, UnsupportedSubjectOutcome):
                    raise TypeError(f"INN {item['inn']}: legal entity risk calculation was rejected")
                _, summary_reused = get_or_create_summary_v3(
                    session,
                    risk.assessment_id,
                    generated_at=calculated_at,
                )
                counters["risk_reused" if risk_reused else "risk_created"] += 1
                counters["summary_reused" if summary_reused else "summary_created"] += 1
    finally:
        engine.dispose()
    return counters


def require_exportable(cursor, selected: list[dict], *, created_at: datetime) -> None:
    publication = PublicationInfo(
        schema_version=SCHEMA_VERSION,
        release_id="canonical-cohort-selection",
        published_at=created_at,
        result_date=created_at.date(),
        content_updated_at=created_at,
        index_eligible=False,
    )
    for item in selected:
        projection = build_projection(cursor, item["inn"], publication)
        if projection.company.inn != item["inn"]:
            raise ValueError(f"INN {item['inn']}: projection identity mismatch")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-main-sha")
    parser.add_argument("--created-at")
    parser.add_argument("--persist-risk-summary", action="store_true")
    parser.add_argument(
        "--supersedes-release-id",
        default="public-v1-20260925T030601Z-ec8a39b4",
    )
    args = parser.parse_args()
    if not args.database_url:
        parser.error("DATABASE_URL or --database-url is required")

    source_sha = args.source_main_sha or subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True
    ).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("source main SHA must be a full Git SHA")
    created_at = (
        datetime.fromisoformat(args.created_at)
        if args.created_at
        else datetime.now(UTC)
    )
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("created-at must include a timezone")

    url = args.database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(url, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        with connection.cursor() as cursor:
            selected = select_candidates(cursor)

    persistence = {"risk_created": 0, "risk_reused": 0, "summary_created": 0, "summary_reused": 0}
    if args.persist_risk_summary:
        persistence = persist_risk_summary(
            args.database_url,
            selected,
            calculated_at=created_at,
        )

    with psycopg.connect(url, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        with connection.cursor() as cursor:
            require_exportable(cursor, selected, created_at=created_at)

    manifest = CanonicalManifest(
        schema_version="canonical-public-cohort-v1",
        manifest_version=2,
        release_name="nextcompany-canonical-operational-cohort-40",
        created_at=created_at,
        source_main_sha=source_sha,
        source_database="nextcompany_operational",
        production_eligibility="VERIFIED_OPERATIONAL",
        selection_policy={
            "version": POLICY_VERSION,
            "candidate_filter": [
                "current operational Master",
                "legal entity only",
                "official fns_egrul master provenance",
                "valid INN and OGRN",
                "production build_projection passes",
            ],
            "ranking": ["usable persisted source coverage DESC", "INN ASC"],
            "coverage_sources": sorted(SOURCE_TABLES),
            "risk_outcome_used": False,
        },
        supersedes_release_id=args.supersedes_release_id,
        entities=tuple(
            ManifestEntity(**{key: value for key, value in item.items() if key != "id"})
            for item in selected
        ),
    )
    payload = (
        json.dumps(
            manifest.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise FileExistsError(f"canonical manifest already exists: {args.output}")
    args.output.write_bytes(payload)
    args.output.with_suffix(args.output.suffix + ".sha256").write_text(
        f"{digest}  {args.output.name}\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "manifest": str(args.output),
                "sha256": digest,
                "record_count": 40,
                "persistence": persistence,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

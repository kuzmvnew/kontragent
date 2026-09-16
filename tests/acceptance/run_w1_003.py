"""Disposable W1-003 PostgreSQL/browser fixtures plus official metadata/XSD probe.

This is NOT the six-gate real-source acceptance. It deliberately does not import
12+ million live support facts into a GitHub runner. Full official bulk import,
Master Registry coverage and real-company browser evidence belong to the user's
configured PostgreSQL acceptance command.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import httpx
from sqlalchemy import func, select

from app.database.base import Base
from app.database.postgres import engine, get_session
import app.models  # noqa: F401 - registers all mapped tables
from app.models.company import Company
from app.models.fns_sme_support import FnsSmeSupportEntry
from app.models.source import DataSet, IngestionRun
from app.providers.fns_sme_support_provider import (
    FnsSmeSupportProvider,
    FnsSmeSupportRelease,
)
from app.services.fns_sme_support_registry_service import ensure_fns_sme_support_dataset
from app.services.fns_sme_support_service import get_fns_sme_support_check_for_inn
from scripts.accept_fns_sme_support import browser_check
from scripts.sync_fns_sme_support import sync_fns_sme_support


OUT = Path("data/acceptance/w1-003-ci")
FOUND_INN = "7701234567"
ABSENT_INN = "7812345678"
XSD_NS = "http://www.w3.org/2001/XMLSchema"


def require_disposable_ci():
    if (
        os.getenv("GITHUB_ACTIONS") != "true"
        or engine.url.database != "test"
        or engine.url.host not in {"localhost", "127.0.0.1"}
    ):
        raise RuntimeError(
            "Fixture writes require GitHub-hosted CI and loopback PostgreSQL database 'test'"
        )


def fixture_xml(*, declared_count: int = 2) -> bytes:
    return f'''<?xml version="1.0" encoding="windows-1251"?>
<Файл ИдФайл="fixture" ВерсФорм="4.04" ТипИнф="РМСП_ПП_ПОДДЕРЖ" КолДок="{declared_count}">
  <ИдОтпр ИННЮЛ="7707329152" ДолжОтв="CI" />
  <Документ ИдДок="CI-LEGAL" ДатаСвед="2026-09-15" СрокПод="2026-12-31" ДатаОказ="2026-01-15" ИнфНаруш="2">
    <ИННЮЛ>{FOUND_INN}</ИННЮЛ>
    <ОГРН>1027700000001</ОГРН>
    <ФормПод КодФорм="0001" НаимФорм="SYNTHETIC FINANCIAL SUPPORT" />
    <ВидПод КодВид="0001" НаимВид="SYNTHETIC SUBSIDY" />
    <РазмПод РазмПод="150000.00" ЕдПод="1" />
    <РегДок ИдРД="SYNTHETIC-REG" />
  </Документ>
  <Документ ИдДок="CI-NPD" ДатаСвед="2026-09-15" СрокПод="2026-11-01" ДатаОказ="2026-02-01" ИнфНаруш="2">
    <ИННФЛ>771234567890</ИННФЛ>
    <ФормПод КодФорм="0003" НаимФорм="SYNTHETIC TRAINING" />
    <ВидПод КодВид="0003" НаимВид="SYNTHETIC COURSE" />
    <РазмПод РазмПод="1" ЕдПод="5" />
    <РегДок ИдРД="SYNTHETIC-REG-2" />
  </Документ>
</Файл>
'''.encode("cp1251")


def make_archive(path: Path, *, declared_count: int = 2):
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w") as archive:
        archive.writestr("fixture.xml", fixture_xml(declared_count=declared_count))


def latest_success_fingerprint():
    with get_session() as session:
        dataset = session.scalar(select(DataSet).where(DataSet.code == "fns_sme_support"))
        run = session.scalar(
            select(IngestionRun)
            .where(IngestionRun.dataset_id == dataset.id, IngestionRun.status == "success")
            .order_by(IngestionRun.finished_at.desc().nullslast(), IngestionRun.id.desc())
            .limit(1)
        )
        rows = session.execute(
            select(FnsSmeSupportEntry.source_record_key, FnsSmeSupportEntry.recipient_inn)
            .where(FnsSmeSupportEntry.ingestion_run_id == run.id)
            .order_by(FnsSmeSupportEntry.id)
        ).all()
        return run.id, rows, run.data_date, dict(run.details or {})


def fixture_acceptance():
    require_disposable_ci()
    Base.metadata.create_all(engine)
    ensure_fns_sme_support_dataset()

    with get_session() as session:
        assert session.scalar(select(func.count()).select_from(Company)) == 0
        session.add_all(
            [
                Company(inn=FOUND_INN, name="SYNTHETIC W1-003 FOUND", entity_type="legal"),
                Company(inn=ABSENT_INN, name="SYNTHETIC W1-003 ABSENT", entity_type="legal"),
            ]
        )
        session.commit()

    unavailable = browser_check(
        [{"label": "fixture-unavailable", "inn": FOUND_INN, "result": "unavailable"}],
        OUT,
    )

    archive = OUT / "fixture.zip"
    make_archive(archive)
    release = FnsSmeSupportRelease(
        data_url="https://fixtures.invalid/fns-sme-support.zip",
        structure_url="https://fixtures.invalid/structure.xsd",
        modified_date=date(2026, 9, 1),
        data_date=date(2026, 9, 15),
    )
    imported = sync_fns_sme_support(
        archive_path=archive,
        release=release,
        batch_size=1,
        cleanup_old=False,
    )
    assert imported["source_records"] == 2
    assert imported["eligible_records"] == 1
    assert imported["excluded_npd_records"] == 1
    assert imported["persisted_records"] == 1

    found1 = get_fns_sme_support_check_for_inn(FOUND_INN)
    found2 = get_fns_sme_support_check_for_inn(FOUND_INN)
    absent = get_fns_sme_support_check_for_inn(ABSENT_INN)
    assert found1 == found2 and found1["result"] == "found"
    assert absent["result"] == "not_found" and absent["is_support_recipient"] is False

    pages = browser_check(
        [
            {
                "label": "fixture-found",
                "inn": FOUND_INN,
                "result": "found",
                "text": "SYNTHETIC SUBSIDY",
            },
            {"label": "fixture-not-found", "inn": ABSENT_INN, "result": "not_found"},
        ],
        OUT,
    )

    before = latest_success_fingerprint()
    bad_archive = OUT / "fixture-bad.zip"
    make_archive(bad_archive, declared_count=3)
    try:
        sync_fns_sme_support(
            archive_path=bad_archive,
            release=FnsSmeSupportRelease(
                data_url="https://fixtures.invalid/fns-sme-support-bad.zip",
                structure_url="https://fixtures.invalid/structure.xsd",
                modified_date=date(2026, 9, 2),
                data_date=date(2026, 9, 16),
            ),
            batch_size=1,
            cleanup_old=False,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("Incomplete fixture must fail")
    after = latest_success_fingerprint()
    assert before == after, "Failed run must preserve the previous successful snapshot"
    assert get_fns_sme_support_check_for_inn(FOUND_INN)["result"] == "found"

    with get_session() as session:
        dataset = session.scalar(select(DataSet).where(DataSet.code == "fns_sme_support"))
        latest = session.scalar(
            select(IngestionRun)
            .where(IngestionRun.dataset_id == dataset.id)
            .order_by(IngestionRun.started_at.desc(), IngestionRun.id.desc())
            .limit(1)
        )
        assert latest.status == "failed"
        failed_rows = session.scalar(
            select(func.count()).select_from(FnsSmeSupportEntry).where(
                FnsSmeSupportEntry.ingestion_run_id == latest.id
            )
        )
        assert failed_rows == 0

    return {
        "status": "PASS_SYNTHETIC_FIXTURE_ONLY",
        "person_rows_persisted": False,
        "postgres_write_read": "PASS",
        "not_found_semantics": "PASS",
        "failed_run_preserves_previous_snapshot": "PASS",
        "browser": unavailable + pages,
        "import": imported,
    }


def _official_xsd_graph(start_url: str) -> tuple[set[str], list[dict]]:
    """Read the official XSD plus same-host include/import dependencies.

    FNS schemas may keep declarations in included XSDs or refer to shared
    declarations via ``ref``. Checking only local ``name`` attributes therefore
    produces false failures. We still restrict the probe to official
    file.nalog.ru HTTPS resources and cap traversal.
    """
    pending = [start_url]
    seen: set[str] = set()
    declarations: set[str] = set()
    documents: list[dict] = []

    with httpx.Client(timeout=60, follow_redirects=True) as client:
        while pending:
            url = pending.pop(0)
            if url in seen:
                continue
            if len(seen) >= 25:
                raise AssertionError("Official XSD dependency graph is unexpectedly large")

            parsed_url = urlparse(url)
            if parsed_url.scheme != "https" or parsed_url.hostname != "file.nalog.ru":
                raise AssertionError(f"Unexpected XSD dependency host: {url}")

            response = client.get(url)
            if response.status_code != 200:
                raise AssertionError(f"Official FNS XSD HTTP {response.status_code}: {url}")

            try:
                root = ET.fromstring(response.content)
            except ET.ParseError as error:
                raise AssertionError(f"Official FNS XSD is not valid XML: {url}: {error}") from error

            if root.tag != f"{{{XSD_NS}}}schema":
                raise AssertionError(f"Official FNS structure is not an XML Schema: {url}")

            seen.add(url)
            declaration_count = 0
            for element in root.iter():
                for attribute in ("name", "ref"):
                    value = element.attrib.get(attribute)
                    if value:
                        declarations.add(value.rsplit(":", 1)[-1])
                        declaration_count += 1

            dependencies = []
            for tag in ("include", "import", "redefine"):
                for element in root.findall(f".//{{{XSD_NS}}}{tag}"):
                    location = element.attrib.get("schemaLocation")
                    if not location:
                        continue
                    dependency = urljoin(url, location)
                    dependency_url = urlparse(dependency)
                    if (
                        dependency_url.scheme == "https"
                        and dependency_url.hostname == "file.nalog.ru"
                    ):
                        dependencies.append(dependency)
                        if dependency not in seen and dependency not in pending:
                            pending.append(dependency)

            documents.append(
                {
                    "url": url,
                    "http_status": response.status_code,
                    "declaration_or_ref_count": declaration_count,
                    "dependencies": dependencies,
                }
            )

    return declarations, documents


def official_metadata_probe():
    release = FnsSmeSupportProvider().discover_release()
    if not release.data_url.startswith(
        "https://file.nalog.ru/opendata/7707329152-rsmppp/"
    ):
        raise AssertionError("Unexpected FNS bulk-data host/path")
    if not release.structure_url:
        raise AssertionError("Official FNS metadata did not expose an XSD URL")

    declarations, xsd_documents = _official_xsd_graph(release.structure_url)
    expected_declarations = {
        "КолДок", "Документ", "ИННЮЛ", "ИННФЛ", "ОГРН", "ОГРНИП",
        "ДатаОказ", "СрокПод", "ФормПод", "ВидПод", "РазмПод",
    }
    missing = sorted(expected_declarations - declarations)
    if missing:
        raise AssertionError(
            "Current official XSD graph is missing expected W1-003 declarations: "
            + ", ".join(missing)
        )

    return {
        "status": "PASS_OFFICIAL_METADATA_AND_XSD_GRAPH_ONLY",
        "metadata_url": "https://www.nalog.gov.ru/opendata/7707329152-rsmppp/",
        "data_url": release.data_url,
        "structure_url": release.structure_url,
        "modified_date": str(release.modified_date),
        "data_date": str(release.data_date),
        "xsd_documents": xsd_documents,
        "xsd_expected_declarations": sorted(expected_declarations),
        "bulk_dataset_downloaded": False,
        "real_company": "NOT_CONFIRMED_IN_CI",
    }


def main():
    require_disposable_ci()
    OUT.mkdir(parents=True, exist_ok=True)
    report = {
        "environment": "disposable-github-actions-postgresql-17",
        "user_mac_database": "NOT_ACCESSED",
        "real_bulk_acceptance": "NOT_RUN",
    }
    try:
        report["fixtures"] = fixture_acceptance()
        report["official_metadata"] = official_metadata_probe()
    except Exception as error:
        report["error_type"] = type(error).__name__
        report["error"] = str(error)[:1000]
        status = 1
    else:
        status = 0
    (OUT / "ci-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return status


if __name__ == "__main__":
    raise SystemExit(main())

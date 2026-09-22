from datetime import datetime, timezone
from pathlib import Path

from openpyxl import Workbook
import pytest

from app.ingestion import mintrans_ted_registry as ted
from app.models.mintrans_ted import MINTRANS_TED_FACT_CODE
from app.services import bulk_update_service as bulk
from app.services.bulk_update_service import BulkUpdatePipeline
from app.services.mintrans_ted_registry_service import (
    LIVE_INGESTION,
    build_mintrans_ted_dataset_spec,
    build_mintrans_ted_source_spec,
)


SOURCE_AS_OF = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
HEADERS = ("ИНН", "ОГРН/ОГРНИП", "Регистрационный номер", "Дата включения")
VALID_ROW = ("7707083893", "1027700132195", "ТЭД-0001", "20.09.2026")


def _xlsx(path: Path, rows, *, headers=HEADERS) -> Path:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    workbook.save(path)
    workbook.close()
    return path


def test_valid_xlsx_normalizes_required_fields(tmp_path):
    path = _xlsx(
        tmp_path / "ted.xlsx",
        [
            VALID_ROW,
            (None, "304500116000157", 2, datetime(2026, 9, 21)),
        ],
    )

    parsed = ted.parse_mintrans_ted_xlsx(path, source_as_of=SOURCE_AS_OF)

    assert parsed.record_count == 2
    assert parsed.records_rejected == 0
    assert parsed.payload.entries[0] == {
        "source_row_number": 2,
        "inn": "7707083893",
        "ogrn": "1027700132195",
        "registry_number": "ТЭД-0001",
        "included_at": SOURCE_AS_OF.date(),
        "row_hash": parsed.payload.entries[0]["row_hash"],
        "validation_state": "valid",
    }
    assert parsed.payload.entries[1]["ogrn"] == "304500116000157"
    assert parsed.payload.entries[1]["registry_number"] == "2"


def test_invalid_schema_is_rejected_before_rows_are_read(tmp_path):
    path = _xlsx(
        tmp_path / "bad-schema.xlsx",
        [VALID_ROW[:2]],
        headers=("ИНН", "ОГРН/ОГРНИП"),
    )

    with pytest.raises(ted.MintransTedSchemaError, match="обязательные колонки"):
        ted.parse_mintrans_ted_xlsx(path, source_as_of=SOURCE_AS_OF)


def test_duplicate_rows_are_deduplicated_by_deterministic_row_hash(tmp_path):
    path = _xlsx(tmp_path / "duplicate.xlsx", [VALID_ROW, VALID_ROW])

    parsed = ted.parse_mintrans_ted_xlsx(path, source_as_of=SOURCE_AS_OF)

    assert parsed.record_count == 1
    assert parsed.duplicates == 1
    assert parsed.payload.coverage["duplicate_records"] == 1


def test_invalid_inn_is_quarantined_even_when_ogrn_is_valid(tmp_path):
    path = _xlsx(
        tmp_path / "invalid-inn.xlsx",
        [("7707083894", "1027700132195", "ТЭД-0002", "20.09.2026")],
    )

    parsed = ted.parse_mintrans_ted_xlsx(path, source_as_of=SOURCE_AS_OF)

    assert parsed.record_count == 0
    assert parsed.records_rejected == 1
    assert parsed.payload.quarantine[0]["reason_codes"] == ["invalid_inn"]
    assert parsed.payload.quarantine[0]["source_row_number"] == 2


def test_conflicting_exact_identities_do_not_match_a_company():
    match = ted.resolve_exact_identity(
        inn="7707083893",
        ogrn="1027700132195",
        companies_by_inn={"7707083893": 10},
        companies_by_ogrn={"1027700132195": 20},
    )

    assert match == ted.IdentityMatch(None, "conflict_identity", None)


def test_exact_inn_match_has_priority_and_ogrn_is_exact_fallback():
    inn_match = ted.resolve_exact_identity(
        inn="7707083893",
        ogrn="1027700132195",
        companies_by_inn={"7707083893": 10},
        companies_by_ogrn={"1027700132195": 10},
    )
    ogrn_match = ted.resolve_exact_identity(
        inn="7707083893",
        ogrn="1027700132195",
        companies_by_inn={},
        companies_by_ogrn={"1027700132195": 10},
    )

    assert inn_match == ted.IdentityMatch(10, "matched", "inn_exact")
    assert ogrn_match == ted.IdentityMatch(10, "matched", "ogrn_exact")


def test_unmatched_identity_stays_unmatched_without_negative_fact():
    match = ted.resolve_exact_identity(
        inn="7707083893",
        ogrn="1027700132195",
        companies_by_inn={},
        companies_by_ogrn={},
    )

    assert match == ted.IdentityMatch(None, "unmatched", None)


def test_retry_same_artifact_reuses_content_addressed_raw_and_manifest(tmp_path):
    source = _xlsx(tmp_path / "fixture.xlsx", [VALID_ROW])
    store = tmp_path / "raw"
    first = ted.stage_fixture_artifact(
        source,
        artifact_store=store,
        source_as_of=SOURCE_AS_OF,
        source_metadata={"fixture_id": "DEV-007"},
    )
    artifact_mtime = first.path.stat().st_mtime_ns
    manifest_mtime = first.manifest_path.stat().st_mtime_ns

    second = ted.stage_fixture_artifact(
        source,
        artifact_store=store,
        source_as_of=SOURCE_AS_OF,
        source_metadata={"fixture_id": "DEV-007"},
    )

    assert second == first
    assert second.path.stat().st_mtime_ns == artifact_mtime
    assert second.manifest_path.stat().st_mtime_ns == manifest_mtime
    assert second.manifest["sha256"] == second.checksum
    assert second.manifest["source_metadata"]["live_ingestion"] is False


def test_existing_artifact_manifest_cannot_be_relabelled(tmp_path):
    source = _xlsx(tmp_path / "fixture.xlsx", [VALID_ROW])
    store = tmp_path / "raw"
    ted.stage_fixture_artifact(
        source,
        artifact_store=store,
        source_as_of=SOURCE_AS_OF,
        source_metadata={"fixture_id": "first"},
    )

    with pytest.raises(ted.ArtifactImmutabilityError):
        ted.stage_fixture_artifact(
            source,
            artifact_store=store,
            source_as_of=SOURCE_AS_OF,
            source_metadata={"fixture_id": "changed"},
        )


def test_reparse_is_deterministic(tmp_path):
    path = _xlsx(
        tmp_path / "deterministic.xlsx",
        [VALID_ROW, ("bad", None, "ТЭД-0002", "not-a-date")],
    )

    first = ted.parse_mintrans_ted_xlsx(path, source_as_of=SOURCE_AS_OF)
    second = ted.parse_mintrans_ted_xlsx(path, source_as_of=SOURCE_AS_OF)

    assert second == first


class _Dataset:
    id = 42


class _Session:
    def __init__(self):
        self.scalar_calls = 0
        self.added = []
        self.committed = False
        self.rolled_back = False

    def scalar(self, _statement):
        self.scalar_calls += 1
        return _Dataset() if self.scalar_calls == 1 else None

    def add(self, value):
        self.added.append(value)

    def flush(self):
        for value in self.added:
            value.id = 7

    def execute(self, _statement):
        return None

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


def test_publication_failure_rolls_back_before_active_pointer_changes(monkeypatch, tmp_path):
    path = _xlsx(tmp_path / "rollback.xlsx", [VALID_ROW])
    session = _Session()
    events = []
    monkeypatch.setattr(bulk, "acquire_dataset_lock", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        bulk, "release_dataset_lock", lambda *args, **kwargs: events.append("released")
    )
    monkeypatch.setattr(bulk, "start_update_run", lambda *args, **kwargs: 11)
    monkeypatch.setattr(
        bulk, "finish_update_run", lambda *args, **kwargs: events.append("success")
    )
    monkeypatch.setattr(
        bulk, "fail_update_run", lambda *args, **kwargs: events.append("failed")
    )
    monkeypatch.setattr(bulk, "get_session", lambda: session)

    def broken_publication(_session, parsed):
        assert parsed.entries[0]["registry_number"] == "ТЭД-0001"
        raise RuntimeError("fact projection failed")

    pipeline = BulkUpdatePipeline(
        download=lambda: path,
        parse=lambda fixture: ted.parse_mintrans_ted_xlsx(
            fixture, source_as_of=SOURCE_AS_OF
        ),
        publish=broken_publication,
    )

    with pytest.raises(RuntimeError, match="fact projection failed"):
        pipeline.run("mintrans_ted_registry", trigger="fixture")

    assert session.rolled_back is True
    assert session.committed is False
    assert events == ["failed", "released"]


def test_fact_projection_uses_canonical_code_and_exact_match_evidence():
    entry = {
        "inn": "7707083893",
        "ogrn": "1027700132195",
        "registry_number": "ТЭД-0001",
        "included_at": SOURCE_AS_OF.date(),
        "row_hash": "a" * 64,
    }

    value, evidence = ted.build_registry_listing_fact(
        entry,
        match_method="inn_exact",
        artifact_sha256="b" * 64,
    )

    assert MINTRANS_TED_FACT_CODE == "transport_forwarding.registry_listing"
    assert value == {
        "listed": True,
        "registry_number": "ТЭД-0001",
        "included_at": "2026-09-20",
    }
    assert evidence["matching_method"] == "inn_exact"
    assert evidence["negative_inference"] is False


def test_registry_metadata_keeps_fixture_source_disabled():
    source = build_mintrans_ted_source_spec()
    dataset = build_mintrans_ted_dataset_spec(source_id=1)

    assert LIVE_INGESTION is False
    assert source["enabled"] is False
    assert dataset["enabled"] is False
    assert dataset["auto_update_status"] == "not_configured"
    assert dataset["refresh_schedule"] == "manual"

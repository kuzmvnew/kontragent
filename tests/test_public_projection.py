from __future__ import annotations

import ast
import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from public_app.contracts import (
    CanonicalManifest,
    ManifestEntity,
    PublicationInfo,
    PublicProjection,
    PublicState,
    scan_forbidden,
    strongest_state,
)
from scripts.export_public_release import build_projection, release_id_for
from scripts.select_public_cohort import rank_candidates
from tests.public_test_support import legal_inn, projection


def entities(count: int) -> tuple[ManifestEntity, ...]:
    return tuple(
        ManifestEntity(
            inn=legal_inn(200_000_000 + index),
            entity_type="legal",
            master_dataset="fns_egrul",
            source="fns",
            usable_source_coverage=("REVEXP",),
        )
        for index in range(count)
    )


def manifest(items: tuple[ManifestEntity, ...]) -> CanonicalManifest:
    return CanonicalManifest(
        schema_version="canonical-public-cohort-v1",
        manifest_version=2,
        release_name="test-cohort",
        created_at=datetime(2026, 9, 25, tzinfo=UTC),
        source_main_sha="6dc86fdd2911d3e681a085fdd10e5665b0bcf855",
        source_database="nextcompany_operational",
        production_eligibility="VERIFIED_OPERATIONAL",
        selection_policy={"version": "test-v1", "risk_outcome_used": False},
        entities=items,
    )


def test_public_projection_forbids_extra_fields():
    value = projection().model_dump(mode="json")
    value["company"]["raw_payload"] = {"secret": True}
    with pytest.raises(ValidationError):
        PublicProjection.model_validate(value)


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ((PublicState.FOUND, PublicState.STALE_DATA), PublicState.STALE_DATA),
        ((PublicState.NOT_FOUND, PublicState.SOURCE_UNAVAILABLE), PublicState.SOURCE_UNAVAILABLE),
        ((PublicState.FOUND, PublicState.CONFLICTING_EVIDENCE), PublicState.CONFLICTING_EVIDENCE),
        ((PublicState.FOUND, PublicState.NOT_FOUND), PublicState.FOUND),
    ],
)
def test_limiting_state_precedence(states, expected):
    assert strongest_state(*states) == expected


@pytest.mark.parametrize(
    "value",
    [
        {"worker_run": "123"},
        {"nested": {"authorization": "Bearer value"}},
        {"safe": "/Users/person/private.xml"},
        {"artifact_reference": "s3://bucket/file"},
    ],
)
def test_forbidden_public_values_are_rejected(value):
    with pytest.raises(ValueError):
        scan_forbidden(value)


@pytest.mark.parametrize("count", [40, 500, 1_000])
def test_manifest_contract_supports_future_bounded_cohort_growth(count):
    assert len(manifest(entities(count)).entities) == count


def test_manifest_rejects_duplicate_inn():
    items = list(entities(40))
    items[-1] = items[0]
    with pytest.raises(ValidationError):
        manifest(tuple(items))


def test_manifest_rejects_individual_entrepreneur_inn():
    with pytest.raises(ValidationError):
        ManifestEntity(
            inn="500100732259",
            entity_type="legal",
            master_dataset="fns_egrul",
            source="fns",
        )


def test_canonical_manifest_is_exactly_40_real_legal_inns():
    path = Path(__file__).resolve().parents[1] / "docs/releases/public-v1-cohort-40.json"
    parsed = CanonicalManifest.model_validate_json(path.read_bytes())
    assert len(parsed.entities) == len({item.inn for item in parsed.entities}) == 40
    sidecar = path.with_suffix(path.suffix + ".sha256").read_text().split()[0]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == sidecar


def test_legacy_disposable_manifest_cannot_be_production_manifest():
    path = (
        Path(__file__).resolve().parents[1]
        / "docs/releases/superseded/public-v1-20260925T030601Z-ec8a39b4-cohort.json"
    )
    with pytest.raises(ValidationError):
        CanonicalManifest.model_validate_json(path.read_bytes())


def test_cohort_ranking_is_deterministic_and_risk_independent():
    candidates = [
        {"inn": "2000000021", "usable_source_coverage": ("A",)},
        {"inn": "2000000013", "usable_source_coverage": ("A", "B")},
        {"inn": "2000000005", "usable_source_coverage": ("A", "B")},
    ]
    assert [item["inn"] for item in rank_candidates(candidates)] == [
        "2000000005",
        "2000000013",
        "2000000021",
    ]


def test_old_release_evidence_is_preserved_and_superseded():
    root = Path(__file__).resolve().parents[1]
    evidence = root / "docs/releases/public-v1-20260925T030601Z-ec8a39b4.superseded.json"
    assert "SUPERSEDED_NOT_PRODUCTION_ELIGIBLE" in evidence.read_text(encoding="utf-8")


def test_cohort_change_forces_a_new_default_release_id():
    now = datetime(2026, 9, 25, 7, 0, tzinfo=UTC)
    source_sha = "6dc86fdd2911d3e681a085fdd10e5665b0bcf855"
    first = release_id_for(now, source_sha, "1" * 64)
    second = release_id_for(now, source_sha, "2" * 64)
    assert first != second


def test_missing_company_fails_closed_before_projection():
    class EmptyCursor:
        def execute(self, _query, _params):
            return None

        def fetchone(self):
            return None

    now = datetime(2026, 9, 25, 7, 0, tzinfo=UTC)
    publication = PublicationInfo(
        schema_version="public-projection-v1",
        release_id="missing-company-test",
        published_at=now,
        result_date=now.date(),
        content_updated_at=now,
        index_eligible=False,
    )
    with pytest.raises(ValueError, match="absent from Master"):
        build_projection(EmptyCursor(), "0274101890", publication)


def test_public_app_has_no_operational_imports():
    root = Path(__file__).resolve().parents[1] / "public_app"
    forbidden = ("app.ingestion", "app.providers", "app.worker", "app.aggregators", "app.services", "app.models")
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert not any(name.startswith(forbidden) for name in names), (path, names)

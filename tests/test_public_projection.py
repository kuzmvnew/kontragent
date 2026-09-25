from __future__ import annotations

from pathlib import Path
import ast

import pytest
from pydantic import ValidationError

from public_app.contracts import (
    CanonicalManifest,
    ManifestEntity,
    PublicProjection,
    PublicState,
    scan_forbidden,
    strongest_state,
)
from tests.public_test_support import legal_inn, projection


def entities(count: int) -> tuple[ManifestEntity, ...]:
    return tuple(ManifestEntity(inn=legal_inn(200_000_000 + index), entity_type="legal") for index in range(count))


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


@pytest.mark.parametrize("count", [39, 41])
def test_manifest_requires_exactly_40(count):
    with pytest.raises(ValidationError):
        CanonicalManifest(manifest_version=1, release_name="bad", entities=entities(count))


def test_manifest_rejects_duplicate_inn():
    items = list(entities(40))
    items[-1] = items[0]
    with pytest.raises(ValidationError):
        CanonicalManifest(manifest_version=1, release_name="duplicate", entities=tuple(items))


def test_manifest_rejects_individual_entrepreneur_inn():
    with pytest.raises(ValidationError):
        ManifestEntity(inn="500100732259", entity_type="legal")


def test_canonical_manifest_is_exactly_40_real_legal_inns():
    path = Path(__file__).resolve().parents[1] / "config/public_release_40.json"
    manifest = CanonicalManifest.model_validate_json(path.read_bytes())
    assert len(manifest.entities) == len({item.inn for item in manifest.entities}) == 40


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

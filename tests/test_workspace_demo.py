from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from public_app.main import create_app as create_public_app
from scripts.public_release_common import load_bundle
from scripts.workspace_demo_support import (
    DEMO_COHORT,
    DEMO_RELEASE_ID,
    DEMO_SOURCE_CLASS,
    build_demo_bundle,
    reset_demo,
    validate_demo_database_url,
    validate_demo_topology,
)
from workspace_app.main import create_app as create_workspace_app


class DemoProjectionRepository:
    def __init__(self, projections):
        self.items = {item.company.inn: item for item in projections}

    def ready(self):
        return True, DEMO_RELEASE_ID, len(self.items)

    def get_company(self, inn):
        return self.items.get(inn)

    def search(self, query, limit=20):
        normalized = query.casefold()
        return [
            item
            for item in self.items.values()
            if query in item.company.inn or normalized in item.company.name.casefold()
        ][:limit]

    def sitemap_rows(self):
        raise AssertionError("Demo sitemap must not query indexable rows")


def test_demo_database_safety_refuses_remote_and_production_names():
    assert validate_demo_database_url(
        "postgresql+psycopg://demo:secret@127.0.0.1/nextcompany_demo_operational",
        label="DATABASE_URL",
    )
    with pytest.raises(ValueError, match="localhost"):
        validate_demo_database_url(
            "postgresql://demo:secret@db.example/nextcompany_demo",
            label="DATABASE_URL",
        )
    with pytest.raises(ValueError, match="contain 'demo'"):
        validate_demo_database_url(
            "postgresql://demo:secret@localhost/nextcompany",
            label="DATABASE_URL",
        )
    with pytest.raises(ValueError, match="must be separate"):
        validate_demo_topology(
            operational_url="postgresql://demo:secret@localhost/shared_demo",
            public_import_url="postgresql://demo:secret@localhost/shared_demo",
        )
    with pytest.raises(ValueError, match="LOCAL_DEMO_ONLY"):
        reset_demo(
            operational_url="postgresql://localhost/nextcompany_demo_operational",
            public_import_url="postgresql://localhost/nextcompany_demo_public",
            confirmation="wrong-token",
        )


def test_demo_bundle_is_deterministic_safe_and_importer_compatible(tmp_path):
    first = build_demo_bundle(tmp_path / "first")
    second = build_demo_bundle(tmp_path / "second")
    assert (first / "manifest.json").read_bytes() == (second / "manifest.json").read_bytes()
    assert (first / "companies.jsonl.gz").read_bytes() == (
        second / "companies.jsonl.gz"
    ).read_bytes()
    manifest, projections, _manifest_sha = load_bundle(first)
    assert manifest.release_id == DEMO_RELEASE_ID
    assert manifest.record_count == len(DEMO_COHORT) == len(projections)
    assert all(not item.publication.index_eligible for item in projections)
    assert all(source.state.value == "NOT_CHECKED" for item in projections for source in item.sources)
    assert all(source.values == {} for item in projections for source in item.sources)
    assert all(
        fact.source.source_class == DEMO_SOURCE_CLASS
        for item in projections
        for section in item.company_view.sections
        for fact in section.items
    )
    with gzip.open(first / "companies.jsonl.gz", "rt", encoding="utf-8") as stream:
        raw = [json.loads(line) for line in stream]
    rendered = json.dumps(raw, ensure_ascii=False)
    assert "СИНТЕТИЧЕСКАЯ ДЕМО" in rendered
    assert '"index_eligible": false' in rendered


def test_demo_mode_banner_and_public_noindex_are_explicit(monkeypatch, tmp_path):
    bundle = build_demo_bundle(tmp_path / "bundle")
    _manifest, projections, _manifest_sha = load_bundle(bundle)
    repository = DemoProjectionRepository(projections)
    monkeypatch.setenv("NEXTCOMPANY_DEMO_MODE", "1")
    monkeypatch.delenv("PUBLIC_FORCE_NOINDEX", raising=False)
    public = TestClient(
        create_public_app(repository, workspace_origin="http://127.0.0.1:8081")
    )
    response = public.get(f"/companies/{DEMO_COHORT[0].inn}")
    assert response.status_code == 200
    assert "Демо-среда · синтетические данные" in response.text
    assert response.headers["x-robots-tag"] == "noindex, nofollow, nosnippet"
    assert response.headers["cache-control"] == "no-store"
    assert f"http://127.0.0.1:8081/login?return_to=%2Fapp%2Fcompanies%2F{DEMO_COHORT[0].inn}" in response.text
    sitemap = public.get("/sitemap.xml")
    assert sitemap.status_code == 200
    assert "<url>" not in sitemap.text


def test_workspace_demo_banner_and_mode_off_regression(monkeypatch):
    monkeypatch.setenv("NEXTCOMPANY_DEMO_MODE", "1")
    demo = TestClient(create_workspace_app(public_origin="http://127.0.0.1:8080"))
    response = demo.get("/login")
    assert response.status_code == 200
    assert "Демо-среда · синтетические данные" in response.text

    monkeypatch.delenv("NEXTCOMPANY_DEMO_MODE", raising=False)
    ordinary = TestClient(create_workspace_app(public_origin="http://127.0.0.1:8080"))
    ordinary_response = ordinary.get("/login")
    assert ordinary_response.status_code == 200
    assert "Демо-среда · синтетические данные" not in ordinary_response.text


def test_demo_runtime_never_imports_test_helpers():
    root = Path(__file__).resolve().parents[1]
    paths = (
        root / "scripts/workspace_demo_support.py",
        root / "scripts/bootstrap_workspace_demo.py",
        root / "scripts/demo_workspace.py",
        root / "scripts/run_demo_stack.py",
    )
    forbidden = ("tests.", "FakePublicRepository", "BrowserBulkRepository")
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert all(marker not in source for marker in forbidden), path


def test_workspace_templates_have_no_dead_primary_controls():
    root = Path(__file__).resolve().parents[1] / "workspace_app/templates"
    rendered = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.html"))
    assert 'href="#"' not in rendered
    assert "href='javascript:void(0)'" not in rendered
    assert 'href="javascript:void(0)' not in rendered

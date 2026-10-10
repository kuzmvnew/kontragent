from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from public_app.main import create_app as create_public_app
from scripts.local_real_preview_support import (
    OPERATIONAL_DATABASE,
    PUBLIC_DATABASE,
    SOURCE_DATABASE,
    require_preview_mode,
    validate_database_topology,
)
from tests.public_test_support import projection
from workspace_app.main import create_app as create_workspace_app


class Repository:
    def __init__(self, item):
        self.item = item

    def get_company(self, inn):
        return self.item if inn == self.item.company.inn else None

    def search(self, query, limit=20):
        return [self.item] if query in {self.item.company.inn, self.item.company.name} else []


def _url(name: str) -> str:
    return f"postgresql+psycopg://localhost/{name}?host=/tmp"


def test_real_preview_guard_requires_explicit_non_demo_mode():
    with pytest.raises(ValueError, match="required"):
        require_preview_mode({})
    with pytest.raises(ValueError, match="synthetic demo"):
        require_preview_mode(
            {
                "NEXTCOMPANY_LOCAL_REAL_PREVIEW": "1",
                "NEXTCOMPANY_DEMO_MODE": "1",
            }
        )
    require_preview_mode(
        {
            "NEXTCOMPANY_LOCAL_REAL_PREVIEW": "1",
            "NEXTCOMPANY_DEMO_MODE": "0",
        }
    )


def test_real_preview_guard_accepts_only_fixed_distinct_local_databases():
    assert validate_database_topology(
        source_url=_url(SOURCE_DATABASE),
        operational_url=_url(OPERATIONAL_DATABASE),
        public_url=_url(PUBLIC_DATABASE),
    ) == {
        "source": SOURCE_DATABASE,
        "operational": OPERATIONAL_DATABASE,
        "public": PUBLIC_DATABASE,
    }
    with pytest.raises(ValueError, match="must be exactly"):
        validate_database_topology(
            source_url=_url(SOURCE_DATABASE),
            operational_url=_url("kontragent"),
            public_url=_url(PUBLIC_DATABASE),
        )
    with pytest.raises(ValueError, match="database rejected"):
        validate_database_topology(
            source_url=f"postgresql+psycopg://production.example/{SOURCE_DATABASE}",
            operational_url=_url(OPERATIONAL_DATABASE),
            public_url=_url(PUBLIC_DATABASE),
            environment={},
        )


def test_public_preview_is_noindex_and_not_labeled_synthetic(monkeypatch):
    monkeypatch.setenv("NEXTCOMPANY_LOCAL_REAL_PREVIEW", "1")
    monkeypatch.setenv("NEXTCOMPANY_DEMO_MODE", "0")
    item = projection(sequence=100_700_101)
    response = TestClient(create_public_app(Repository(item))).get(
        f"/companies/{item.company.inn}"
    )
    assert response.status_code == 200
    assert response.headers["x-robots-tag"] == "noindex, nofollow, nosnippet"
    assert 'data-preview-mode="local-real"' in response.text
    assert "Локальный просмотр реальных данных" in response.text
    assert "LOCAL REAL DATA PREVIEW" not in response.text
    assert "синтетические данные" not in response.text


def test_workspace_preview_has_distinct_banner_and_monitoring_warning(monkeypatch):
    monkeypatch.setenv("NEXTCOMPANY_LOCAL_REAL_PREVIEW", "1")
    monkeypatch.setenv("NEXTCOMPANY_DEMO_MODE", "0")
    response = TestClient(
        create_workspace_app(public_repository=object(), session_factory=object())
    ).get("/login")
    assert response.status_code == 200
    assert 'data-preview-mode="local-real"' in response.text
    assert "Локальный просмотр реальных данных" in response.text
    assert "LOCAL REAL DATA PREVIEW" not in response.text
    assert "синтетические данные" not in response.text


def test_authorized_card_exposes_same_company_view_revision_contract():
    template = (
        __import__("pathlib")
        .Path("workspace_app/templates/company.html")
        .read_text(encoding="utf-8")
    )
    assert 'data-view-contract="{{ projection.company_view.contract_version }}"' in template
    assert 'data-view-revision="{{ projection.company_view.revision }}"' in template
    assert "отсутствие событий не означает отсутствия изменений" in template

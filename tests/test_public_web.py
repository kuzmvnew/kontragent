from __future__ import annotations

import socket
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from public_app.main import create_app
from public_app.contracts import PublicProjection
from tests.public_test_support import projection


class FakeRepository:
    def __init__(self):
        self.item = projection()
        self.write_count = 0

    def active_release(self):
        return {"release_id": self.item.publication.release_id, "record_count": 40}

    def get_company(self, inn):
        return self.item if inn == self.item.company.inn else None

    def search(self, query, limit=20):
        return [self.item] if query.casefold() in self.item.company.name.casefold() else []

    def sitemap_rows(self):
        return [{"inn": self.item.company.inn, "content_updated_at": datetime(2026, 9, 25, tzinfo=timezone.utc)}]

    def ready(self):
        return True, self.item.publication.release_id, 40


def client():
    repository = FakeRepository()
    return TestClient(create_app(repository)), repository


def test_search_full_inn_redirects_to_canonical_card():
    web, _ = client()
    response = web.get("/search?q=0274101890", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == "/companies/0274101890"


def test_search_by_name_and_search_is_noindex():
    web, _ = client()
    response = web.get("/search?q=ТЕСТ")
    assert response.status_code == 200
    assert "ООО ТЕСТ" in response.text
    assert response.headers["x-robots-tag"] == "noindex, follow"


def test_legacy_route_is_permanent_redirect():
    web, _ = client()
    response = web.get("/company/0274101890", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == "/companies/0274101890"


def test_api_and_html_use_same_active_revision_and_show_v3_dates():
    web, repository = client()
    html = web.get("/companies/0274101890")
    api = web.get("/api/company/0274101890")
    assert html.status_code == api.status_code == 200
    assert repository.item.publication.release_id in html.text
    assert api.json()["publication"]["release_id"] == repository.item.publication.release_id
    assert "Risk v3" in html.text
    assert repository.item.summary.short_conclusion in html.text
    assert "Дата данных источника" in html.text
    assert "Дата результата" in html.text


def test_public_gets_make_no_network_calls_or_writes(monkeypatch):
    web, repository = client()

    def blocked(*_args, **_kwargs):
        raise AssertionError("external network attempted")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    for path in ("/", "/companies/0274101890", "/api/company/0274101890", "/robots.txt", "/sitemap.xml"):
        assert web.get(path).status_code == 200
    assert repository.write_count == 0


def test_limiting_states_are_visually_distinct():
    web, repository = client()
    response = web.get(f"/companies/{repository.item.company.inn}")
    assert "state-partial" in response.text
    assert "PARTIAL" in response.text


def test_stale_unavailable_and_unknown_never_become_no_violations():
    web, repository = client()
    value = repository.item.model_dump(mode="json")
    replacements = (
        ("STALE_DATA", "STALE", "Срок актуальности данных истёк."),
        ("SOURCE_UNAVAILABLE", "UNKNOWN", "Источник временно недоступен."),
        ("UNKNOWN", "UNKNOWN", "Результата недостаточно для вывода."),
    )
    for source, (state, freshness, limitation) in zip(value["sources"], replacements):
        source.update({"state": state, "freshness": freshness, "limitation": limitation})
    repository.item = PublicProjection.model_validate(value)
    response = web.get(f"/companies/{repository.item.company.inn}")
    assert response.status_code == 200
    for css_state in ("state-stale_data", "state-source_unavailable", "state-unknown"):
        assert css_state in response.text
    assert "нарушений нет" not in response.text.casefold()


def test_robots_sitemap_canonical_open_graph_jsonld_and_404():
    web, repository = client()
    robots = web.get("/robots.txt")
    assert "Disallow: /search" in robots.text
    assert "Disallow: /api/" in robots.text
    assert "Sitemap: https://nextcompany.pro/sitemap.xml" in robots.text
    sitemap = web.get("/sitemap.xml")
    assert f"/companies/{repository.item.company.inn}" in sitemap.text
    card = web.get(f"/companies/{repository.item.company.inn}")
    assert f'<link rel="canonical" href="https://nextcompany.pro/companies/{repository.item.company.inn}">' in card.text
    assert 'property="og:title"' in card.text
    assert 'type="application/ld+json"' in card.text
    missing = web.get("/companies/7700000000")
    assert missing.status_code == 404
    assert "noindex" in missing.headers["x-robots-tag"]


def test_docs_openapi_and_internal_are_absent():
    web, _ = client()
    for path in ("/docs", "/redoc", "/openapi.json", "/internal/worker"):
        assert web.get(path).status_code == 404


def test_worker_offline_does_not_affect_public_app(monkeypatch):
    monkeypatch.delenv("HOME_WORKER_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    web, _ = client()
    assert web.get("/api/ready").json()["record_count"] == 40
    assert web.get("/companies/0274101890").status_code == 200


def test_api_security_headers_and_no_cors():
    web, _ = client()
    response = web.get("/api/company/0274101890")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-robots-tag"] == "noindex, nofollow, nosnippet"
    assert "access-control-allow-origin" not in response.headers
    health = web.get("/api/health").json()
    assert health == {"status": "ok"}

from __future__ import annotations

import json
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


def test_api_and_html_show_same_prepared_projection_dates_without_engine_metadata():
    web, repository = client()
    html = web.get("/companies/0274101890")
    api = web.get("/api/company/0274101890")
    assert html.status_code == api.status_code == 200
    assert repository.item.publication.release_id not in html.text
    assert "release_id" not in api.json()["publication"]
    assert "Risk v3" not in html.text
    assert repository.item.public_conclusion in html.text
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
    assert "state-attention" in response.text
    assert "Оценка содержит ограничения" in response.text
    assert "PARTIAL" not in response.text


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
    for public_text in ("Данные устарели", "Источник временно недоступен", "Недостаточно данных"):
        assert public_text in response.text
    for internal_text in ("STALE_DATA", "SOURCE_UNAVAILABLE", "state-stale_data", "state-source_unavailable"):
        assert internal_text not in response.text
    assert "нарушений нет" not in response.text.casefold()
    assert "data-nosnippet" in response.text


def test_robots_sitemap_canonical_open_graph_jsonld_and_404():
    web, repository = client()
    robots = web.get("/robots.txt")
    assert "Allow: /" in robots.text
    assert "Disallow: /api/" not in robots.text
    assert "Disallow: /admin/" in robots.text
    assert "Sitemap: https://nextcompany.pro/sitemap.xml" in robots.text
    sitemap = web.get("/sitemap.xml")
    assert "<sitemapindex" in sitemap.text
    assert sitemap.text.count("/sitemaps/companies-") == 16
    shard = __import__("hashlib").sha256(repository.item.company.inn.encode()).hexdigest()[0]
    company_sitemap = web.get(f"/sitemaps/companies-0{shard}.xml.gz")
    assert f"/companies/{repository.item.company.inn}" in company_sitemap.text
    card = web.get(f"/companies/{repository.item.company.inn}")
    assert f'<link rel="canonical" href="https://nextcompany.pro/companies/{repository.item.company.inn}">' in card.text
    assert 'property="og:title"' in card.text
    assert 'type="application/ld+json"' in card.text
    missing = web.get("/companies/7700000000")
    assert missing.status_code == 404
    assert "noindex" in missing.headers["x-robots-tag"]


def test_catalog_is_crawlable_and_query_variants_are_noindex():
    web, repository = client()
    response = web.get("/companies")
    assert response.status_code == 200
    assert f'href="/companies/{repository.item.company.inn}"' in response.text
    assert '<meta name="robots" content="index, follow">' in response.text
    variant = web.get("/companies?sort=name")
    assert variant.status_code == 200
    assert variant.headers["x-robots-tag"] == "noindex, follow"
    assert '<meta name="robots" content="noindex, follow">' in variant.text
    first = web.get("/companies/page/1", follow_redirects=False)
    assert first.status_code == 308
    assert first.headers["location"] == "/companies"
    assert web.get("/companies/page/2").status_code == 404


def test_company_url_normalization_and_tracking_parameters():
    web, repository = client()
    inn = repository.item.company.inn
    trailing = web.get(f"/companies/{inn}/", follow_redirects=False)
    assert trailing.status_code == 308
    assert trailing.headers["location"] == f"/companies/{inn}"
    tracked = web.get(f"/companies/{inn}?utm_source=test&gclid=abc", follow_redirects=False)
    assert tracked.status_code == 308
    assert tracked.headers["location"] == f"/companies/{inn}"
    variant = web.get(f"/companies/{inn}?view=compact")
    assert variant.status_code == 200
    assert variant.headers["x-robots-tag"] == "noindex, follow"
    assert web.get("/company/123", follow_redirects=False).status_code == 404


def test_production_host_normalizes_http_www_and_legacy_path_in_one_hop():
    repository = FakeRepository()
    web = TestClient(create_app(repository), base_url="http://www.nextcompany.pro")
    inn = repository.item.company.inn
    response = web.get(f"/company/{inn}?utm_source=test", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == f"https://nextcompany.pro/companies/{inn}"


def test_numeric_index_payload_is_noindex_sanitized_and_absent_from_discovery():
    web, repository = client()
    value = repository.item.model_dump(mode="json")
    value["sources"][0]["values"]["next_index"] = 81
    repository.item = PublicProjection.model_validate(value)
    inn = repository.item.company.inn
    card = web.get(f"/companies/{inn}")
    assert card.status_code == 200
    assert card.headers["x-robots-tag"] == "noindex, follow"
    assert "next_index" not in card.text
    assert "next_index" not in json.dumps(web.get(f"/api/company/{inn}").json())
    shard = __import__("hashlib").sha256(inn.encode()).hexdigest()[0]
    assert f"/companies/{inn}" not in web.get(f"/sitemaps/companies-0{shard}.xml.gz").text
    assert web.get("/companies").status_code == 404


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

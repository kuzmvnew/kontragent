from datetime import date

from app.services.seo_indexation_service import (
    build_robots_txt,
    build_sitemap,
    build_sitemap_index,
    canonical_company_url,
    split_urls,
)


def test_canonical_company_url():
    assert canonical_company_url(
        "https://nextprofile.ru",
        "ООО Ромашка",
        "6671234567",
    ) == "https://nextprofile.ru/company/ооо-ромашка-6671234567"


def test_split_urls_for_mvp_batches():
    urls = [f"https://example.ru/company/{i}" for i in range(5001)]
    chunks = split_urls(urls, chunk_size=2000)
    assert [len(chunk) for chunk in chunks] == [2000, 2000, 1001]


def test_build_sitemap_contains_loc_and_real_lastmod():
    xml = build_sitemap(
        [
            {
                "loc": "https://example.ru/company/romashka-6671234567",
                "lastmod": date(2026, 9, 16),
            }
        ]
    )
    assert "<urlset" in xml
    assert "https://example.ru/company/romashka-6671234567" in xml
    assert "2026-09-16" in xml


def test_build_sitemap_index():
    xml = build_sitemap_index(
        [
            {
                "loc": "https://example.ru/sitemap-companies-0001.xml",
                "lastmod": "2026-09-16",
            }
        ]
    )
    assert "<sitemapindex" in xml
    assert "sitemap-companies-0001.xml" in xml


def test_build_robots_txt_exposes_sitemap_and_blocks_api_search_noise():
    robots = build_robots_txt(base_url="https://example.ru")
    assert "Sitemap: https://example.ru/sitemap-index.xml" in robots
    assert "Disallow: /api/" in robots
    assert "Disallow: /search?" in robots

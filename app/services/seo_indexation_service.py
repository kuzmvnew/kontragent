from datetime import date
from html import escape
from urllib.parse import urljoin


SITEMAP_URL_LIMIT = 50_000


def canonical_company_url(base_url: str, slug: str, inn: str) -> str:
    base = base_url.rstrip("/") + "/"
    safe_slug = "-".join((slug or "company").strip().lower().split())
    return urljoin(base, f"company/{safe_slug}-{inn}")


def split_urls(urls: list[str], chunk_size: int = 2000) -> list[list[str]]:
    if chunk_size <= 0 or chunk_size > SITEMAP_URL_LIMIT:
        raise ValueError("chunk_size must be between 1 and 50000")
    return [urls[i : i + chunk_size] for i in range(0, len(urls), chunk_size)]


def build_sitemap(urls: list[dict]) -> str:
    if len(urls) > SITEMAP_URL_LIMIT:
        raise ValueError("A sitemap may contain at most 50000 URLs")

    rows = []
    for item in urls:
        loc = escape(str(item["loc"]), quote=True)
        lastmod = item.get("lastmod")
        block = ["  <url>", f"    <loc>{loc}</loc>"]
        if lastmod:
            if isinstance(lastmod, date):
                lastmod = lastmod.isoformat()
            block.append(f"    <lastmod>{escape(str(lastmod))}</lastmod>")
        block.append("  </url>")
        rows.append("\n".join(block))

    body = "\n".join(rows)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{body}\n"
        "</urlset>\n"
    )


def build_sitemap_index(sitemaps: list[dict]) -> str:
    rows = []
    for item in sitemaps:
        loc = escape(str(item["loc"]), quote=True)
        lastmod = item.get("lastmod")
        block = ["  <sitemap>", f"    <loc>{loc}</loc>"]
        if lastmod:
            if isinstance(lastmod, date):
                lastmod = lastmod.isoformat()
            block.append(f"    <lastmod>{escape(str(lastmod))}</lastmod>")
        block.append("  </sitemap>")
        rows.append("\n".join(block))

    body = "\n".join(rows)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{body}\n"
        "</sitemapindex>\n"
    )


def build_robots_txt(*, base_url: str, sitemap_path: str = "/sitemap-index.xml") -> str:
    sitemap_url = base_url.rstrip("/") + sitemap_path
    return (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /api/\n"
        "Disallow: /search?\n"
        f"Sitemap: {sitemap_url}\n"
    )

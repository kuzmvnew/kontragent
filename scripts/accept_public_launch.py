#!/usr/bin/env python3
"""Production acceptance for the active nextcompany.pro public release."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from public_app.contracts import PublicProjection  # noqa: E402
from scripts.public_release_common import load_bundle  # noqa: E402


DEFAULT_INNS = ("0274101890", "7720831611", "9102309919")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://nextcompany.pro")
    parser.add_argument("--www-url", default="https://www.nextcompany.pro")
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--inn", action="append", dest="inns")
    parser.add_argument("--worker-offline-proof", action="store_true")
    args = parser.parse_args()
    inns = tuple(args.inns or DEFAULT_INNS)
    require(len(inns) >= 3, "at least three sample INNs are required")
    base = args.base_url.rstrip("/")
    with httpx.Client(timeout=15, follow_redirects=False) as client:
        landing = client.get(base + "/")
        require(landing.status_code == 200, "landing is not 200")
        require("NEXT Company" in landing.text, "landing brand missing")
        www = client.get(args.www_url.rstrip("/") + "/")
        require(www.status_code in {301, 308}, "www does not redirect")
        require(www.headers.get("location", "").startswith(base), "www redirect is not apex")
        ready = client.get(base + "/api/ready")
        require(ready.status_code == 200, "ready is not 200")
        ready_json = ready.json()
        require(ready_json.get("record_count") == 40, "active release is not 40 records")
        release_id = ready_json.get("release_id")
        robots = client.get(base + "/robots.txt")
        require(robots.status_code == 200 and "Disallow: /api/" in robots.text, "robots invalid")
        sitemap = client.get(base + "/sitemap.xml")
        require(sitemap.status_code == 200 and "<urlset" in sitemap.text, "sitemap invalid")
        missing = client.get(base + "/companies/7700000000")
        require(missing.status_code == 404 and "noindex" in missing.headers.get("x-robots-tag", ""), "404 invalid")
        for inn in inns:
            search = client.get(base + "/search", params={"q": inn})
            require(search.status_code == 308 and search.headers["location"] == f"/companies/{inn}", f"search redirect failed for {inn}")
            card = client.get(f"{base}/companies/{inn}")
            require(card.status_code == 200, f"card failed for {inn}")
            require(f'rel="canonical" href="{base}/companies/{inn}"' in card.text, "canonical missing")
            require('application/ld+json' in card.text and "Дата данных источника" in card.text and "Дата результата" in card.text, "SEO or dates missing")
            api = client.get(f"{base}/api/company/{inn}")
            require(api.status_code == 200, f"API failed for {inn}")
            projection = PublicProjection.model_validate(api.json())
            require(projection.publication.release_id == release_id, "API revision mismatch")
            require(projection.risk.title and projection.summary.short_conclusion, "Risk/Summary missing")
            require(len(projection.sources) == 4, "source blocks missing")
    if args.bundle:
        manifest, projections, _ = load_bundle(args.bundle)
        require(manifest.release_id == release_id, "active release differs from bundle")
        require({item.company.inn for item in projections} >= set(inns), "samples absent from manifest")
    report = {
        "status": "PASS",
        "release_id": release_id,
        "record_count": 40,
        "sample_inns": inns,
        "worker_offline": "PASS" if args.worker_offline_proof else "NOT_PROVEN",
    }
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

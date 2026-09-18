#!/usr/bin/env python3
"""Desktop/mobile Chromium acceptance for the generated Golden-40 report."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright


GROUPS = (
    "Банкротство", "Налоговые факторы", "Действующие",
    "Низкий наблюдаемый риск — кандидат",
)


def verify(page, *, url: str, screenshot: Path) -> dict:
    errors = []
    page.on("console", lambda message: errors.append(f"console:{message.text}") if message.type == "error" else None)
    page.on("pageerror", lambda error: errors.append(f"page:{error}"))
    page.goto(url, wait_until="load")
    cards = page.locator(".card")
    assert cards.count() == 40
    assert page.locator("#count").inner_text() == "40 из 40"
    for index in range(40):
        card = cards.nth(index)
        assert card.locator(".scores b").count() == 3
        assert card.locator("h3", has_text="Главные причины").count() == 1
        assert card.locator("h3", has_text="Ограничения").count() == 1
        assert card.locator("details table").count() == 1
    page.locator("#q").fill("0274101890")
    assert page.locator("#count").inner_text() == "1 из 40"
    assert page.locator(".card:not(.hidden)").count() == 1
    page.locator("#q").fill("")
    filters = {}
    for group in GROUPS:
        page.get_by_role("button", name=group, exact=True).click()
        filters[group] = page.locator(".card:not(.hidden)").count()
        assert filters[group] == 10
    page.get_by_role("button", name="Все", exact=True).click()
    page.locator(".card").first.locator("summary").click()
    assert page.locator(".card").first.locator("details").get_attribute("open") is not None
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(screenshot), full_page=True)
    assert not errors
    return {
        "viewport": page.viewport_size, "cards": 40, "search_result": "1 из 40",
        "filter_counts": filters, "risk_coverage_workflow_fields_per_card": 3,
        "source_tables": 40, "responsive_horizontal_overflow": False,
        "console_errors": errors, "screenshot": str(screenshot),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--html", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    url = args.html.resolve().as_uri()
    screenshots = args.output.parent / "browser"
    screenshots.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            results = {}
            for label, viewport in (
                ("desktop", {"width": 1440, "height": 1000}),
                ("mobile", {"width": 390, "height": 844}),
            ):
                context = browser.new_context(viewport=viewport)
                try:
                    results[label] = verify(
                        context.new_page(), url=url,
                        screenshot=screenshots / f"{label}.png",
                    )
                finally:
                    context.close()
            artifact = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "evidence_class": "VERIFIED_RUNTIME", "browser": "Chromium",
                "browser_version": browser.version, "results": results,
            }
        finally:
            browser.close()
    args.output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "browser": artifact["browser"], "version": artifact["browser_version"],
        "desktop_cards": results["desktop"]["cards"],
        "mobile_cards": results["mobile"]["cards"], "console_errors": 0,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()

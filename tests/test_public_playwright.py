from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psycopg
import pytest

playwright = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright

from scripts.import_public_release import import_release
from tests.test_public_release_postgres import bundle


TEST_URL = os.getenv("PUBLIC_TEST_DATABASE_URL")
WEB_URL = os.getenv("PUBLIC_TEST_WEB_DATABASE_URL") or TEST_URL
pytestmark = pytest.mark.skipif(not TEST_URL, reason="PUBLIC_TEST_DATABASE_URL is not configured")


@pytest.fixture(scope="module")
def public_server(tmp_path_factory):
    temp = tmp_path_factory.mktemp("public-browser")
    release = bundle(temp, "public-v1-browser-test")
    with psycopg.connect(TEST_URL) as connection:
        connection.execute("DELETE FROM public_publication_state")
        connection.execute("DELETE FROM public_company_projections")
        connection.execute("DELETE FROM public_releases")
        connection.execute("INSERT INTO public_publication_state(singleton,active_release_id) VALUES(TRUE,NULL)")
    with psycopg.connect(TEST_URL) as connection:
        import_release(connection, release)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.update(
        {
            "PUBLIC_DATABASE_URL": WEB_URL,
            "PUBLIC_ORIGIN": f"http://127.0.0.1:{port}",
            "PUBLIC_TRUSTED_HOSTS": "127.0.0.1,localhost",
            "PYTHONPATH": str(root),
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "public_app.main:app", "--host", "127.0.0.1", "--port", str(port), "--no-access-log"],
        cwd=root,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            if httpx.get(base + "/api/ready", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    else:
        process.terminate()
        raise RuntimeError("public test server did not become ready")
    yield base
    process.terminate()
    process.wait(timeout=10)


def test_desktop_card_v2(public_server):
    with sync_playwright() as manager:
        browser = manager.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        response = page.goto(public_server + "/companies/1000000002", wait_until="networkidle")
        assert response.status == 200
        assert page.locator("h1").inner_text().startswith("ООО ТЕСТ")
        assert page.locator(".source-card").count() == 4
        assert page.get_by_text("Дата данных источника").count() == 4
        assert page.locator("meta[name=robots]").get_attribute("content") == "index, follow"
        browser.close()


def test_mobile_card_v2_has_no_horizontal_overflow(public_server):
    with sync_playwright() as manager:
        browser = manager.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
        page.goto(public_server + "/companies/1000000002", wait_until="networkidle")
        assert page.locator(".identity-grid").count() == 1
        assert page.locator(".source-card").count() == 4
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        browser.close()

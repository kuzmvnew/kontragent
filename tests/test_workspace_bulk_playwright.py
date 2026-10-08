from __future__ import annotations

import csv
import io
import re
from uuid import uuid4

from playwright.sync_api import expect, sync_playwright
from sqlalchemy.orm import Session

from app.database.postgres import SessionLocal, engine
from app.models.company import Company
from tests.public_test_support import projection
from tests.test_workspace_p0 import _bootstrap, _cleanup
from tests.test_workspace_vertical_slice_playwright import LiveServer, _login
from workspace_app.main import create_app


class BrowserBulkRepository:
    def __init__(self, items, release_id: str):
        self.items = {item.company.inn: item for item in items}
        self.release_id = release_id

    def active_release(self):
        return {
            "release_id": self.release_id,
            "schema_version": "public-projection-v1",
            "record_count": len(self.items),
            "actual_record_count": len(self.items),
        }

    def get_companies(self, inns, *, release_id):
        assert release_id == self.release_id
        return [self.items[inn] for inn in inns if inn in self.items]

    def get_company(self, inn):
        return self.items.get(inn)

    def search(self, query, limit=20):
        return [item for item in self.items.values() if query in item.company.inn or query.casefold() in item.company.name.casefold()][:limit]


def _bulk_csv(items) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["inn"])
    for item in items:
        writer.writerow([item.company.inn])
    return output.getvalue().encode("utf-8-sig")


def test_browser_bulk_multichunk_filter_card_exports_cancel_resume_and_history():
    email = f"browser-bulk-{uuid4()}@example.test"
    release_id = "public-release-browser-bulk"
    items = tuple(projection(470_000_000 + index, release_id=release_id) for index in range(1, 81))
    repository = BrowserBulkRepository(items, release_id)
    try:
        with Session(engine) as session:
            session.add_all(Company(inn=item.company.inn, name=item.company.name, entity_type="legal") for item in items)
            session.commit()
        _bootstrap(email, "Browser Bulk")
        workspace = LiveServer(create_app(public_repository=repository, session_factory=SessionLocal))
        content = _bulk_csv(items)
        with workspace, sync_playwright() as manager:
            browser = manager.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900}, java_script_enabled=False)
            page_errors: list[str] = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))

            page.goto(f"{workspace.url}/login")
            _login(page, email)
            page.get_by_role("link", name="Массовая проверка", exact=True).click()
            expect(page.get_by_text("Пока нет массовых проверок")).to_be_visible()
            page.get_by_label("CSV-файл").set_input_files({"name": "bulk-browser.csv", "mimeType": "text/csv", "buffer": content})
            page.get_by_role("button", name="Создать Bulk Job").click()
            expect(page).to_have_url(re.compile(rf"{re.escape(workspace.url)}/app/bulk/[0-9a-f-]+"))
            first_job_url = page.url
            expect(page.locator("[data-job-status='READY']")).to_be_visible()

            page.get_by_role("button", name="Обработать следующий блок").click()
            expect(page.locator("[data-job-status='RUNNING']")).to_be_visible()
            expect(page.get_by_role("heading", name="75 из 80")).to_be_visible()
            page.get_by_role("button", name="Обработать следующий блок").click()
            expect(page.locator("[data-job-status='COMPLETED']")).to_be_visible()
            expect(page.get_by_role("heading", name="80 из 80")).to_be_visible()

            page.get_by_label("Статус").select_option("READY")
            page.get_by_role("button", name="Фильтровать").click()
            expect(page.locator("tr[data-item-status='READY']")).to_have_count(50)
            page.locator("tr[data-item-status='READY']").first.get_by_role("link", name="Открыть компанию").click()
            expect(page.locator(".company-head")).to_have_attribute("data-company-inn", items[0].company.inn)
            page.go_back()
            expect(page).to_have_url(re.compile(r"/app/bulk/[0-9a-f-]+"))

            with page.expect_download() as csv_download_info:
                page.get_by_role("link", name="Скачать CSV").click()
            exported_csv = csv_download_info.value.path().read_text(encoding="utf-8-sig")
            assert release_id in exported_csv and "READY" in exported_csv
            with page.expect_download() as json_download_info:
                page.get_by_role("link", name="Скачать JSON").click()
            assert release_id in json_download_info.value.path().read_text(encoding="utf-8")

            page.get_by_role("link", name="Массовая проверка", exact=True).click()
            expect(page.locator("[data-bulk-job-id]")).to_have_count(1)
            page.get_by_label("CSV-файл").set_input_files({"name": "bulk-cancel.csv", "mimeType": "text/csv", "buffer": content})
            page.get_by_role("button", name="Создать Bulk Job").click()
            page.get_by_role("button", name="Обработать следующий блок").click()
            expect(page.get_by_role("heading", name="75 из 80")).to_be_visible()
            page.get_by_role("button", name="Отменить").click()
            expect(page.locator("[data-job-status='CANCELLED']")).to_be_visible()
            expect(page.get_by_text("Отменено").locator("strong")).to_have_text("5")
            with page.expect_download() as cancelled_download_info:
                page.get_by_role("link", name="Скачать CSV").click()
            assert "CANCELLED" in cancelled_download_info.value.path().read_text(encoding="utf-8-sig")
            page.get_by_role("button", name="Возобновить").click()
            expect(page.locator("[data-job-status='READY']")).to_be_visible()
            page.get_by_role("button", name="Обработать следующий блок").click()
            expect(page.locator("[data-job-status='COMPLETED']")).to_be_visible()
            expect(page.get_by_role("heading", name="80 из 80")).to_be_visible()

            page.get_by_role("link", name="К истории").click()
            expect(page.locator("[data-bulk-job-id]")).to_have_count(2)
            expect(page.locator(f'a[href="{first_job_url.removeprefix(workspace.url)}"]')).to_have_count(1)
            page.get_by_role("button", name="Выйти").click()
            expect(page).to_have_url(f"{workspace.url}/login")
            assert page_errors == []
            browser.close()
    finally:
        _cleanup(email, inns=tuple(item.company.inn for item in items))

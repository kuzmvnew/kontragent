from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
from pathlib import Path
from uuid import UUID

import httpx
import psycopg
import pytest
import sqlalchemy as sa
import uvicorn
from sqlalchemy.orm import Session

playwright = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect, sync_playwright

from app.database.postgres import SessionLocal, engine
from app.models.monitoring import MonitoringEvent, MonitoringSubscription, WorkspaceFeedEntry
from app.models.workspace import (
    CustomerUser,
    SavedCompany,
    Workspace,
    WorkspaceBulkItem,
    WorkspaceBulkJob,
    WorkspaceInvitation,
    WorkspaceMembership,
    WorkspaceReport,
    WorkspaceRole,
)
from public_app.main import create_app as create_public_app
from public_app.repository import PublicRepository
from scripts.import_public_release import import_release
from scripts.public_release_common import load_bundle
from scripts.workspace_demo_support import (
    DEMO_COHORT,
    DEMO_EXPECTED_SHA_ENV,
    DEMO_MEMBER_EMAIL,
    DEMO_OWNER_EMAIL,
    DEMO_RELEASE_ID,
    DEMO_WORKSPACE_NAME,
    EXPECTED_OPERATIONAL_HEAD,
    EXPECTED_PUBLIC_HEAD,
    RESET_CONFIRMATION,
    advance_demo_event,
    bootstrap_demo,
    build_demo_bundle,
    demo_acceptance_truth,
    resolve_demo_source_sha,
    reset_demo,
)
from workspace_app.main import create_app as create_workspace_app


RUN_E2E = os.getenv("WORKSPACE_DEMO_E2E") == "1"
OPERATIONAL_URL = os.getenv("DATABASE_URL", "")
PUBLIC_IMPORT_URL = os.getenv("PUBLIC_IMPORT_DATABASE_URL", "")
PUBLIC_WEB_URL = os.getenv("PUBLIC_DATABASE_URL", "")
DEMO_PASSWORD = os.getenv("NEXTCOMPANY_DEMO_PASSWORD", "")
EXPECTED_SOURCE_SHA = os.getenv(DEMO_EXPECTED_SHA_ENV, "")
pytestmark = pytest.mark.skipif(
    not (
        RUN_E2E
        and OPERATIONAL_URL
        and PUBLIC_IMPORT_URL
        and PUBLIC_WEB_URL
        and DEMO_PASSWORD
        and EXPECTED_SOURCE_SHA
    ),
    reason="Workspace Demo E2E environment is not configured",
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class LiveServer:
    def __init__(self, app, port: int, ready_path: str) -> None:
        self.url = f"http://127.0.0.1:{port}"
        self.ready_path = ready_path
        self.server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                log_level="error",
                access_log=False,
                lifespan="off",
            )
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(120):
            try:
                if httpx.get(self.url + self.ready_path, timeout=0.5).status_code < 500:
                    return self
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        self.server.should_exit = True
        self.thread.join(timeout=5)
        raise RuntimeError(f"Demo server did not start at {self.url}")

    def __exit__(self, *_args) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
        assert not self.thread.is_alive(), f"orphan Demo server at {self.url}"


def _login(page, email: str, password: str) -> None:
    page.get_by_label("Email").fill(email)
    page.get_by_label("Пароль").fill(password)
    page.get_by_role("button", name="Войти").click()


def _screenshot(page, directory: Path, name: str) -> None:
    page.screenshot(path=directory / f"{name}.png", full_page=True)


def _db_truth(workspace_id) -> dict[str, int]:
    with Session(engine) as session:
        return {
            "members": int(
                session.scalar(
                    sa.select(sa.func.count())
                    .select_from(WorkspaceMembership)
                    .where(
                        WorkspaceMembership.workspace_id == workspace_id,
                        WorkspaceMembership.status == "active",
                    )
                )
                or 0
            ),
            "pending_invitations": int(
                session.scalar(
                    sa.select(sa.func.count())
                    .select_from(WorkspaceInvitation)
                    .where(
                        WorkspaceInvitation.workspace_id == workspace_id,
                        WorkspaceInvitation.status == "PENDING",
                    )
                )
                or 0
            ),
            "saved": int(
                session.scalar(
                    sa.select(sa.func.count())
                    .select_from(SavedCompany)
                    .where(SavedCompany.workspace_id == workspace_id)
                )
                or 0
            ),
            "monitoring": int(
                session.scalar(
                    sa.select(sa.func.count())
                    .select_from(MonitoringSubscription)
                    .where(MonitoringSubscription.workspace_id == workspace_id)
                )
                or 0
            ),
            "feed": int(
                session.scalar(
                    sa.select(sa.func.count())
                    .select_from(WorkspaceFeedEntry)
                    .where(WorkspaceFeedEntry.workspace_id == workspace_id)
                )
                or 0
            ),
            "reports": int(
                session.scalar(
                    sa.select(sa.func.count())
                    .select_from(WorkspaceReport)
                    .where(WorkspaceReport.workspace_id == workspace_id)
                )
                or 0
            ),
            "bulk": int(
                session.scalar(
                    sa.select(sa.func.count())
                    .select_from(WorkspaceBulkJob)
                    .where(WorkspaceBulkJob.workspace_id == workspace_id)
                )
                or 0
            ),
        }


def test_workspace_demo_full_product_e2e(tmp_path):
    actual_git_sha = resolve_demo_source_sha()
    assert actual_git_sha == EXPECTED_SOURCE_SHA
    artifacts = Path(os.getenv("DEMO_E2E_ARTIFACT_DIR", str(tmp_path / "demo-artifacts")))
    artifacts.mkdir(parents=True, exist_ok=True)
    secret_values = {
        DEMO_PASSWORD,
        OPERATIONAL_URL,
        PUBLIC_IMPORT_URL,
        PUBLIC_WEB_URL,
    }
    reset_demo(
        operational_url=OPERATIONAL_URL,
        public_import_url=PUBLIC_IMPORT_URL,
        confirmation=RESET_CONFIRMATION,
    )
    import_url = PUBLIC_IMPORT_URL.replace(
        "postgresql+psycopg://", "postgresql://", 1
    )
    previous_source_sha = "0" * 40 if actual_git_sha != "0" * 40 else "1" * 40
    with tempfile.TemporaryDirectory(prefix="nextcompany-old-demo-") as directory:
        previous_bundle = build_demo_bundle(
            Path(directory), source_sha=previous_source_sha
        )
        with psycopg.connect(import_url) as connection:
            import_release(connection, previous_bundle, DEMO_RELEASE_ID)
    with pytest.raises(RuntimeError, match="Demo reset required"):
        bootstrap_demo(
            operational_url=OPERATIONAL_URL,
            public_import_url=PUBLIC_IMPORT_URL,
            public_web_url=PUBLIC_WEB_URL,
            password=DEMO_PASSWORD,
            profile="clean",
        )
    reset_demo(
        operational_url=OPERATIONAL_URL,
        public_import_url=PUBLIC_IMPORT_URL,
        confirmation=RESET_CONFIRMATION,
    )
    first = bootstrap_demo(
        operational_url=OPERATIONAL_URL,
        public_import_url=PUBLIC_IMPORT_URL,
        public_web_url=PUBLIC_WEB_URL,
        password=DEMO_PASSWORD,
        profile="clean",
    )
    second = bootstrap_demo(
        operational_url=OPERATIONAL_URL,
        public_import_url=PUBLIC_IMPORT_URL,
        public_web_url=PUBLIC_WEB_URL,
        password=DEMO_PASSWORD,
        profile="clean",
    )
    assert first["status"] == "created"
    assert second["status"] == "already_ready"
    assert first["source_main_sha"] == second["source_main_sha"] == actual_git_sha
    assert first["company_inns"] == second["company_inns"]
    assert first["counts"] == second["counts"]
    unknown_release = "foreign-local-release"
    with psycopg.connect(import_url) as connection:
        connection.execute(
            "UPDATE public_publication_state SET active_release_id=NULL WHERE singleton=TRUE"
        )
        connection.execute(
            "UPDATE public_releases SET status='rolled_back' WHERE release_id=%s",
            (DEMO_RELEASE_ID,),
        )
        connection.execute(
            """
            INSERT INTO public_releases(
                release_id, schema_version, source_main_sha, previous_release_id,
                created_at, record_count, manifest_sha256, status
            ) VALUES (%s, 'public-projection-v1', %s, NULL, now(), 1, %s, 'active')
            """,
            (unknown_release, "0" * 40, "0" * 64),
        )
        connection.execute(
            "UPDATE public_publication_state SET active_release_id=%s WHERE singleton=TRUE",
            (unknown_release,),
        )
    with pytest.raises(RuntimeError, match="unknown release"):
        reset_demo(
            operational_url=OPERATIONAL_URL,
            public_import_url=PUBLIC_IMPORT_URL,
            confirmation=RESET_CONFIRMATION,
        )
    with psycopg.connect(import_url) as connection:
        connection.execute(
            "UPDATE public_publication_state SET active_release_id=NULL WHERE singleton=TRUE"
        )
        connection.execute(
            "DELETE FROM public_releases WHERE release_id=%s", (unknown_release,)
        )
        connection.execute(
            "UPDATE public_releases SET status='active' WHERE release_id=%s",
            (DEMO_RELEASE_ID,),
        )
        connection.execute(
            "UPDATE public_publication_state SET active_release_id=%s WHERE singleton=TRUE",
            (DEMO_RELEASE_ID,),
        )
    reset_demo(
        operational_url=OPERATIONAL_URL,
        public_import_url=PUBLIC_IMPORT_URL,
        confirmation=RESET_CONFIRMATION,
    )
    clean = bootstrap_demo(
        operational_url=OPERATIONAL_URL,
        public_import_url=PUBLIC_IMPORT_URL,
        public_web_url=PUBLIC_WEB_URL,
        password=DEMO_PASSWORD,
        profile="clean",
    )
    assert clean["company_inns"] == first["company_inns"]
    assert clean["source_main_sha"] == actual_git_sha
    workspace_id = UUID(clean["workspace_id"])

    repository = PublicRepository(PUBLIC_WEB_URL)
    public_port, workspace_port = _free_port(), _free_port()
    public_url = f"http://127.0.0.1:{public_port}"
    workspace_url = f"http://127.0.0.1:{workspace_port}"
    workspace = LiveServer(
        create_workspace_app(
            public_repository=repository,
            session_factory=SessionLocal,
            public_origin=public_url,
        ),
        workspace_port,
        "/login",
    )
    public = LiveServer(
        create_public_app(repository, workspace_origin=workspace_url),
        public_port,
        "/api/ready",
    )
    page_errors: list[str] = []
    navigation: dict[str, bool] = {}
    persistence: dict[str, bool] = {}
    rbac_denied = False
    report_payload: dict = {}
    bulk_json: dict = {}

    with workspace, public, sync_playwright() as manager:
        browser = manager.chromium.launch(headless=True)
        owner_context = browser.new_context(
            viewport={"width": 1440, "height": 1000},
            java_script_enabled=False,
        )
        page = owner_context.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))

        page.goto(public.url)
        expect(page.get_by_text("Демо-среда · синтетические данные")).to_be_visible()
        page.get_by_label("Найдите компанию").fill(DEMO_COHORT[0].inn)
        page.get_by_role("button", name="Проверить").click()
        expect(page).to_have_url(f"{public.url}/companies/{DEMO_COHORT[0].inn}")
        expect(page.get_by_role("heading", name=DEMO_COHORT[0].name)).to_be_visible()
        expect(page.get_by_text("Официальные источники не запрашивались", exact=False).first).to_be_visible()
        _screenshot(page, artifacts, "01-public-card")
        page.get_by_role("link", name="Открыть в кабинете").click()
        expect(page).to_have_url(f"{workspace.url}/login?return_to=%2Fapp%2Fcompanies%2F{DEMO_COHORT[0].inn}")
        _login(page, DEMO_OWNER_EMAIL, DEMO_PASSWORD)
        expect(page).to_have_url(f"{workspace.url}/app/companies/{DEMO_COHORT[0].inn}")

        page.get_by_role("link", name="NEXT Company", exact=True).click()
        expect(page.get_by_role("heading", name=DEMO_WORKSPACE_NAME)).to_be_visible()
        expect(page.locator(".workspace-context")).to_contain_text("Владелец")
        _screenshot(page, artifacts, "02-dashboard")
        for label, heading in (
            ("Главная", DEMO_WORKSPACE_NAME),
            ("Поиск", "Найти компанию"),
            ("Сохранённые", "Сохранённые компании"),
            ("Мониторинг", "Мониторинг"),
            ("Отчёты", "Отчёты"),
            ("Массовая проверка", "Массовая проверка"),
            ("Пользователи", "Пользователи"),
            ("Настройки", "Настройки"),
        ):
            page.get_by_role("link", name=label, exact=True).click()
            expect(page.get_by_role("heading", name=heading, exact=True).first).to_be_visible()
            navigation[label] = page.url.startswith(workspace.url + "/app")

        page.get_by_role("link", name="Поиск", exact=True).click()
        page.get_by_label("Название или ИНН").fill(DEMO_COHORT[3].inn)
        page.get_by_role("button", name="Найти").click()
        page.get_by_role("link", name=DEMO_COHORT[3].name).click()
        expect(page.locator(".company-head")).to_have_attribute("data-company-inn", DEMO_COHORT[3].inn)
        _screenshot(page, artifacts, "03-authorized-card")
        page.get_by_role("button", name="Сохранить компанию").click()
        expect(page.locator(".company-head")).to_have_attribute("data-saved", "true")
        with Session(engine) as session:
            persistence["saved"] = session.scalar(
                sa.select(SavedCompany.id)
                .join(CustomerUser, CustomerUser.id == SavedCompany.saved_by_user_id)
                .where(CustomerUser.email == DEMO_OWNER_EMAIL)
            ) is not None

        page.get_by_role("link", name="Сохранённые", exact=True).click()
        page.locator("details.note-editor").first.locator("summary").click()
        note = "Persisted Demo E2E note"
        page.get_by_label("Заметка").fill(note)
        page.get_by_role("button", name="Сохранить заметку").click()
        page.reload()
        expect(page.locator("p.saved-note")).to_have_text(note)
        with Session(engine) as session:
            persistence["note"] = session.scalar(
                sa.select(SavedCompany.note).where(SavedCompany.note == note)
            ) == note

        page.get_by_role("link", name="Мониторинг", exact=True).last.click()
        page.get_by_role("button", name="Включить мониторинг").click()
        expect(page.locator("[data-monitoring-state='ACTIVE']")).to_be_visible()
        with Session(engine) as session:
            company_id = session.scalar(
                sa.select(SavedCompany.company_id).where(SavedCompany.note == note)
            )
            persistence["monitoring"] = session.scalar(
                sa.select(MonitoringSubscription.id).where(
                    MonitoringSubscription.company_id == company_id
                )
            ) is not None
            event_result = advance_demo_event(session, inn=DEMO_COHORT[3].inn)
            session.commit()
        assert event_result["events"] == 1 and event_result["feed_entries"] == 1
        page.get_by_role("link", name="Мониторинг", exact=True).click()
        expect(page.locator(".feed-item[data-read='false']")).to_have_count(1)
        _screenshot(page, artifacts, "04-monitoring")
        page.get_by_role("button", name="Отметить прочитанным").click()
        expect(page.locator(".feed-item[data-read='true']")).to_have_count(1)
        page.get_by_role("button", name="Приостановить").click()
        expect(page.locator("[data-monitoring-state='PAUSED']")).to_be_visible()
        page.get_by_role("button", name="Возобновить").click()
        expect(page.locator("[data-monitoring-state='ACTIVE']")).to_be_visible()
        with Session(engine) as session:
            persistence["monitoring_event"] = int(
                session.scalar(sa.select(sa.func.count()).select_from(MonitoringEvent)) or 0
            ) == 1 and int(
                session.scalar(sa.select(sa.func.count()).select_from(WorkspaceFeedEntry)) or 0
            ) == 1

        page.goto(f"{workspace.url}/app/companies/{DEMO_COHORT[1].inn}")
        page.get_by_role("button", name="Сформировать отчёт").click()
        expect(page.get_by_role("heading", name=DEMO_COHORT[1].name)).to_be_visible()
        _screenshot(page, artifacts, "05-report")
        with page.expect_download() as download_info:
            page.get_by_role("link", name="Скачать JSON").click()
        report_payload = json.loads(download_info.value.path().read_text(encoding="utf-8"))
        assert report_payload["subject"]["inn"] == DEMO_COHORT[1].inn
        assert "workspace_id" not in json.dumps(report_payload)
        assert "company_id" not in json.dumps(report_payload)
        with page.expect_download() as csv_download_info:
            page.get_by_role("link", name="Скачать CSV").click()
        assert DEMO_COHORT[1].inn in csv_download_info.value.path().read_text(encoding="utf-8-sig")
        with Session(engine) as session:
            persistence["report"] = session.scalar(sa.select(WorkspaceReport.id)) is not None

        page.get_by_role("link", name="Массовая проверка", exact=True).click()
        bulk_content = (
            "inn\n"
            f"{DEMO_COHORT[0].inn}\n"
            f"{DEMO_COHORT[4].inn}\n"
            f"{DEMO_COHORT[0].inn}\n"
            "123\n"
        ).encode()
        page.get_by_label("CSV-файл").set_input_files(
            {"name": "demo-e2e.csv", "mimeType": "text/csv", "buffer": bulk_content}
        )
        page.get_by_role("button", name="Создать задание").click()
        page.get_by_role("button", name="Обработать следующий блок").click()
        expect(page.locator("[data-job-status='COMPLETED']")).to_be_visible()
        expect(page.locator("tr[data-item-status='READY']")).to_have_count(2)
        expect(page.locator("tr[data-item-status='DUPLICATE']")).to_have_count(1)
        expect(page.locator("tr[data-item-status='INVALID_INN']")).to_have_count(1)
        _screenshot(page, artifacts, "06-bulk")
        with page.expect_download() as bulk_download_info:
            page.get_by_role("link", name="Скачать JSON").click()
        bulk_json = json.loads(bulk_download_info.value.path().read_text(encoding="utf-8"))
        statuses = {item["status"] for item in bulk_json["items"]}
        assert {"READY", "DUPLICATE", "INVALID_INN"} <= statuses
        with page.expect_download() as bulk_csv_info:
            page.get_by_role("link", name="Скачать CSV").click()
        assert "DUPLICATE" in bulk_csv_info.value.path().read_text(encoding="utf-8-sig")
        with Session(engine) as session:
            persistence["bulk"] = session.scalar(sa.select(WorkspaceBulkJob.id)) is not None
            assert int(session.scalar(sa.select(sa.func.count()).select_from(WorkspaceBulkItem)) or 0) == 4

        page.get_by_role("link", name="Пользователи", exact=True).click()
        page.get_by_label("Email", exact=True).fill(DEMO_MEMBER_EMAIL)
        page.locator("form[action='/app/users/invitations'] select").select_option(
            label="Администратор"
        )
        page.get_by_role("button", name="Создать приглашение").click()
        invite_path = page.locator(".invite-secret code").inner_text()
        assert invite_path.startswith("/invite/")
        secret_values.add(invite_path)
        member_context = browser.new_context(
            viewport={"width": 1280, "height": 900}, java_script_enabled=False
        )
        member_page = member_context.new_page()
        member_page.on("pageerror", lambda error: page_errors.append(str(error)))
        member_page.goto(workspace.url + invite_path)
        member_page.get_by_label("Создайте пароль").fill(DEMO_PASSWORD)
        member_page.get_by_label("Повторите пароль").fill(DEMO_PASSWORD)
        member_page.get_by_role("button", name="Принять приглашение").click()
        expect(member_page).to_have_url(workspace.url + "/app")
        expect(member_page.locator(".workspace-context")).to_contain_text("Администратор")

        page.goto(workspace.url + "/app/users")
        member_row = page.locator("article.member-row").filter(has_text=DEMO_MEMBER_EMAIL)
        member_row.locator("select").select_option(label="Участник")
        member_row.get_by_role("button", name="Сменить роль").click()
        member_page.reload()
        expect(member_page.locator(".workspace-context")).to_contain_text("Участник")
        member_page.goto(workspace.url + "/app/settings")
        member_csrf = member_page.locator("form[action='/logout'] input[name='csrf']").input_value()
        denied = member_context.request.post(
            workspace.url + "/app/settings/workspace",
            form={"csrf": member_csrf, "name": "Forbidden member rename"},
            max_redirects=0,
        )
        rbac_denied = denied.status == 403
        assert rbac_denied
        with Session(engine) as session:
            role_key = session.scalar(
                sa.select(WorkspaceRole.role_key)
                .join(WorkspaceMembership, WorkspaceMembership.role_id == WorkspaceRole.id)
                .join(CustomerUser, CustomerUser.id == WorkspaceMembership.user_id)
                .where(CustomerUser.email == DEMO_MEMBER_EMAIL)
            )
            persistence["invitation_accept_role"] = role_key == "MEMBER"

        page.goto(workspace.url + "/app/users")
        _screenshot(page, artifacts, "07-users")
        page.get_by_role("link", name="Настройки", exact=True).click()
        renamed = "NEXT Company Demo Acceptance"
        page.get_by_label("Название Workspace").fill(renamed)
        page.get_by_role("button", name="Сохранить название").click()
        expect(page.locator(".workspace-context")).to_contain_text(renamed)
        page.reload()
        expect(page.locator(".workspace-context")).to_contain_text(renamed)
        truth = _db_truth(workspace_id)
        expect(
            page.locator(".usage-list article.usage-row")
            .filter(has_text="Сохранённые компании")
            .locator("strong")
        ).to_have_text(str(truth["saved"]))
        expect(
            page.locator(".usage-list article.usage-row")
            .filter(has_text="Отчёты")
            .locator("strong")
        ).to_have_text(str(truth["reports"]))
        with Session(engine) as session:
            persistence["settings"] = session.get(Workspace, workspace_id).name == renamed
        _screenshot(page, artifacts, "08-settings")
        page.get_by_label("Название Workspace").fill(DEMO_WORKSPACE_NAME)
        page.get_by_role("button", name="Сохранить название").click()
        expect(page.locator(".workspace-context")).to_contain_text(DEMO_WORKSPACE_NAME)
        with Session(engine) as session:
            assert session.get(Workspace, workspace_id).name == DEMO_WORKSPACE_NAME

        for context in (owner_context, member_context):
            secret_values.update(
                cookie["value"]
                for cookie in context.cookies()
                if len(cookie.get("value", "")) >= 16
            )
        for active_page in (page, member_page):
            csrf_values = active_page.locator("input[name='csrf']").evaluate_all(
                "elements => elements.map(element => element.value)"
            )
            secret_values.update(value for value in csrf_values if len(value) >= 16)

        page.get_by_role("button", name="Выйти").click()
        expect(page).to_have_url(workspace.url + "/login")
        page.goto(workspace.url + "/app")
        expect(page).to_have_url(workspace.url + "/login?return_to=/app")
        browser.close()

    ready_truth = demo_acceptance_truth(
        operational_url=OPERATIONAL_URL,
        public_import_url=PUBLIC_IMPORT_URL,
        public_web_url=PUBLIC_WEB_URL,
    )
    assert ready_truth["public_repository_ready"] is True
    assert ready_truth["release_id"] == DEMO_RELEASE_ID
    assert ready_truth["repository_count"] == len(DEMO_COHORT)
    assert ready_truth["release_record_count"] == ready_truth["projection_count"]
    assert ready_truth["index_eligible_count"] == 0
    with tempfile.TemporaryDirectory(prefix="nextcompany-exact-demo-") as directory:
        exact_bundle = build_demo_bundle(Path(directory), source_sha=actual_git_sha)
        manifest, _projections, manifest_sha = load_bundle(exact_bundle)
    with psycopg.connect(import_url) as connection:
        release_source_sha, stored_manifest_sha = connection.execute(
            "SELECT source_main_sha, manifest_sha256 FROM public_releases WHERE release_id=%s",
            (DEMO_RELEASE_ID,),
        ).fetchone()
    assert manifest.source_main_sha == actual_git_sha
    assert manifest.cohort_source_main_sha == actual_git_sha
    assert release_source_sha == actual_git_sha
    assert stored_manifest_sha == manifest_sha
    assert ready_truth["public_release_source_main_sha"] == actual_git_sha
    assert ready_truth["public_release_manifest_sha256"] == manifest_sha
    with psycopg.connect(PUBLIC_WEB_URL.replace("postgresql+psycopg://", "postgresql://", 1)) as connection:
        assert connection.execute("SELECT count(*) FROM public_company_projections").fetchone()[0] == len(DEMO_COHORT)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("UPDATE public_publication_state SET updated_at=now()")

    assert all(navigation.values())
    assert all(persistence.values())
    assert not page_errors
    report = {
        "git_sha": actual_git_sha,
        "expected_head_sha": EXPECTED_SOURCE_SHA,
        "manifest_source_main_sha": manifest.source_main_sha,
        "manifest_cohort_source_main_sha": manifest.cohort_source_main_sha,
        "public_release_source_main_sha": release_source_sha,
        "public_release_manifest_sha256": stored_manifest_sha,
        "operational_migration_head": EXPECTED_OPERATIONAL_HEAD,
        "public_migration_head": EXPECTED_PUBLIC_HEAD,
        "demo_release_id": DEMO_RELEASE_ID,
        "demo_cohort_size": len(DEMO_COHORT),
        "public_repository_ready": ready_truth["public_repository_ready"],
        "navigation": navigation,
        "action_persistence": persistence,
        "rbac_denial": rbac_denied,
        "page_errors": page_errors,
        "db_truth": _db_truth(workspace_id),
        "public_readonly": True,
        "artifact_secret_scan": True,
        "production_mutation": False,
    }
    aligned_shas = {
        report["git_sha"],
        report["expected_head_sha"],
        report["manifest_source_main_sha"],
        report["manifest_cohort_source_main_sha"],
        report["public_release_source_main_sha"],
    }
    assert aligned_shas == {actual_git_sha}
    report_path = artifacts / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    secret_values.add("NEXTCOMPANY_DEMO_PASSWORD")
    for artifact in artifacts.iterdir():
        artifact_name = artifact.name.encode("utf-8")
        artifact_content = artifact.read_bytes()
        for secret in secret_values:
            encoded = secret.encode("utf-8")
            assert encoded not in artifact_name
            assert encoded not in artifact_content


def test_workspace_demo_showcase_profile_is_populated_and_idempotent(tmp_path):
    reset_demo(
        operational_url=OPERATIONAL_URL,
        public_import_url=PUBLIC_IMPORT_URL,
        confirmation=RESET_CONFIRMATION,
    )
    first = bootstrap_demo(
        operational_url=OPERATIONAL_URL,
        public_import_url=PUBLIC_IMPORT_URL,
        public_web_url=PUBLIC_WEB_URL,
        password=DEMO_PASSWORD,
        profile="showcase",
    )
    second = bootstrap_demo(
        operational_url=OPERATIONAL_URL,
        public_import_url=PUBLIC_IMPORT_URL,
        public_web_url=PUBLIC_WEB_URL,
        password=DEMO_PASSWORD,
        profile="showcase",
    )
    assert first["status"] == "created"
    assert second["status"] == "already_ready"
    assert first["counts"] == second["counts"]
    assert first["counts"]["members"] == 2
    assert first["counts"]["saved"] >= 2
    assert first["counts"]["monitoring"] == 1
    assert first["counts"]["feed"] == 1
    assert first["counts"]["reports"] == 1
    assert first["counts"]["bulk"] == 1

    port = _free_port()
    workspace = LiveServer(
        create_workspace_app(
            public_repository=PublicRepository(PUBLIC_WEB_URL),
            session_factory=SessionLocal,
            public_origin="http://127.0.0.1:8080",
        ),
        port,
        "/login",
    )
    with workspace, sync_playwright() as manager:
        browser = manager.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto(workspace.url + "/login")
        _login(page, DEMO_OWNER_EMAIL, DEMO_PASSWORD)
        expect(page.get_by_role("heading", name=DEMO_WORKSPACE_NAME)).to_be_visible()
        expect(page.locator(".metrics-grid")).to_contain_text("2")
        artifact_dir = Path(
            os.getenv("DEMO_E2E_ARTIFACT_DIR", str(tmp_path / "demo-artifacts"))
        )
        artifact_dir.mkdir(parents=True, exist_ok=True)
        _screenshot(page, artifact_dir, "09-showcase-dashboard")
        browser.close()

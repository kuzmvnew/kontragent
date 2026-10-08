from __future__ import annotations

import re
from uuid import uuid4

import sqlalchemy as sa
from playwright.sync_api import expect, sync_playwright
from sqlalchemy.orm import Session

from app.database.postgres import SessionLocal, engine
from app.models.workspace import CustomerUser, WorkspaceMembership, WorkspaceRole
from tests.test_workspace_users_settings import (
    EmptyPublicRepository,
    PASSWORD,
    _add_user_membership,
    _bootstrap,
    _purge,
)
from tests.test_workspace_vertical_slice_playwright import LiveServer, _login
from workspace_app.auth import hash_password
from workspace_app.main import create_app


def test_browser_invite_role_change_accept_and_settings_usage():
    owner_email = f"browser-owner-{uuid4()}@example.test"
    member_email = f"browser-member-{uuid4()}@example.test"
    invited_email = f"browser-invited-{uuid4()}@example.test"
    _owner_id, workspace_id = _bootstrap(
        owner_email, "Browser Users", member_limit=5
    )
    _member_id, _membership_id = _add_user_membership(workspace_id, member_email)
    workspace = LiveServer(
        create_app(
            public_repository=EmptyPublicRepository(),
            session_factory=SessionLocal,
        )
    )
    try:
        with workspace, sync_playwright() as manager:
            browser = manager.chromium.launch(headless=True)
            owner_context = browser.new_context(viewport={"width": 1440, "height": 1000})
            owner_page = owner_context.new_page()
            page_errors: list[str] = []
            owner_page.on("pageerror", lambda error: page_errors.append(str(error)))
            owner_page.goto(f"{workspace.url}/login")
            _login(owner_page, owner_email)
            owner_page.get_by_role("link", name="Пользователи", exact=True).click()
            expect(owner_page.get_by_role("heading", name="Пользователи")).to_be_visible()

            member_row = owner_page.locator("article.member-row").filter(has_text=member_email)
            member_row.locator("select").select_option(label="Администратор")
            member_row.get_by_role("button", name="Сменить роль").click()
            expect(
                owner_page.locator("article.member-row").filter(has_text=member_email).locator("select")
            ).to_have_value(
                re.compile(r"[0-9a-f-]{36}")
            )
            with Session(engine) as session:
                member_role = session.scalar(
                    sa.select(WorkspaceRole.role_key)
                    .join(WorkspaceMembership, WorkspaceMembership.role_id == WorkspaceRole.id)
                    .join(CustomerUser, CustomerUser.id == WorkspaceMembership.user_id)
                    .where(
                        WorkspaceMembership.workspace_id == workspace_id,
                        CustomerUser.email == member_email,
                    )
                )
                assert member_role == "ADMIN"

            owner_page.get_by_label("Email", exact=True).fill(invited_email)
            owner_page.locator("form[action='/app/users/invitations'] select").select_option(
                label="Администратор (ADMIN)"
            )
            owner_page.get_by_role("button", name="Создать приглашение").click()
            expect(owner_page.get_by_text("Ссылка приглашения показана один раз")).to_be_visible()
            invite_path = owner_page.locator(".invite-secret code").inner_text()
            assert invite_path.startswith("/invite/")

            invited_context = browser.new_context(viewport={"width": 1280, "height": 900})
            invited_page = invited_context.new_page()
            invited_page.on("pageerror", lambda error: page_errors.append(str(error)))
            invited_page.goto(f"{workspace.url}{invite_path}")
            invited_page.get_by_label("Создайте пароль").fill(PASSWORD)
            invited_page.get_by_label("Повторите пароль").fill(PASSWORD)
            invited_page.get_by_role("button", name="Принять приглашение").click()
            expect(invited_page).to_have_url(f"{workspace.url}/app")
            expect(invited_page.locator(".workspace-context")).to_contain_text("Администратор")

            invited_page.get_by_role("link", name="Настройки", exact=True).click()
            invited_page.get_by_label("Название Workspace").fill("Renamed in Chromium")
            invited_page.get_by_role("button", name="Сохранить название").click()
            expect(invited_page.locator(".workspace-context")).to_contain_text(
                "Renamed in Chromium"
            )
            invited_page.reload()
            expect(invited_page.get_by_role("heading", name="Настройки")).to_be_visible()
            expect(invited_page.locator(".workspace-context")).to_contain_text(
                "Renamed in Chromium"
            )
            expect(invited_page.get_by_text("3", exact=True).first).to_be_visible()
            assert page_errors == []
            browser.close()
    finally:
        _purge((workspace_id,), (owner_email, member_email, invited_email))


def test_browser_existing_user_invitation_requires_current_password():
    owner_email = f"browser-owner-{uuid4()}@example.test"
    existing_email = f"browser-existing-{uuid4()}@example.test"
    _owner_id, workspace_id = _bootstrap(owner_email, "Existing Browser")
    original_hash = hash_password(PASSWORD)
    with Session(engine) as session:
        session.add(
            CustomerUser(
                id=uuid4(),
                email=existing_email,
                password_hash=original_hash,
                status="active",
            )
        )
        session.commit()
    workspace = LiveServer(
        create_app(
            public_repository=EmptyPublicRepository(),
            session_factory=SessionLocal,
        )
    )
    try:
        with workspace, sync_playwright() as manager:
            browser = manager.chromium.launch(headless=True)
            owner_page = browser.new_page()
            owner_page.goto(f"{workspace.url}/login")
            _login(owner_page, owner_email)
            owner_page.get_by_role("link", name="Пользователи", exact=True).click()
            owner_page.get_by_label("Email", exact=True).fill(existing_email)
            owner_page.locator("form[action='/app/users/invitations'] select").select_option(
                label="Участник (MEMBER)"
            )
            owner_page.get_by_role("button", name="Создать приглашение").click()
            invite_path = owner_page.locator(".invite-secret code").inner_text()

            context = browser.new_context()
            page = context.new_page()
            page.goto(f"{workspace.url}{invite_path}")
            expect(page.get_by_label("Текущий пароль аккаунта")).to_be_visible()
            page.get_by_label("Текущий пароль аккаунта").fill("wrong-current-password")
            page.get_by_role("button", name="Принять приглашение").click()
            expect(page.get_by_role("alert")).to_contain_text(
                "Не удалось подтвердить приглашение"
            )
            page.get_by_label("Текущий пароль аккаунта").fill(PASSWORD)
            page.get_by_role("button", name="Принять приглашение").click()
            expect(page).to_have_url(f"{workspace.url}/app")
            with Session(engine) as session:
                user = session.scalar(
                    sa.select(CustomerUser).where(CustomerUser.email == existing_email)
                )
                assert user.password_hash == original_hash
            browser.close()
    finally:
        _purge((workspace_id,), (owner_email, existing_email))

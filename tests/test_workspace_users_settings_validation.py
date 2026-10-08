"""Raw-input validation at the service and real Workspace HTTP boundaries."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database.postgres import SessionLocal, engine
from app.models.workspace import Workspace, WorkspaceAuditEvent, WorkspaceInvitation
from tests.test_workspace_users_settings import (
    EmptyPublicRepository,
    PASSWORD,
    _add_user_membership,
    _bootstrap,
    _csrf,
    _purge,
    _role_id,
)
from workspace_app.auth import normalize_email
from workspace_app.main import create_app
from workspace_app.member_service import create_invitation
from workspace_app.service import ActionDenied, authenticate_customer_attempt
from workspace_app.settings_service import get_workspace_usage, update_workspace_name


BAD_EMAILS = (
    "user\u0000@example.test",
    "user\u0001@example.test",
    "user\u001f@example.test",
    "user\u007f@example.test",
    "user\u0085@example.test",
    "user\u200b@example.test",
    "user\ufeff@example.test",
    "\u0001user@example.test",
    "user\u0001name@example.test",
    "user@example\u0001.test",
    "user@example.test\u0001",
    "user@example.test\u200b",
    "bad\ud800@example.test",
)
BAD_NAMES = (
    "bad\u0000name",
    "bad\u0001name",
    "bad\u001fname",
    "bad\u007fname",
    "bad\u0085name",
    "bad\u200bname",
    "bad\ufeffname",
    "\u0001name",
    "name\u0001",
    "bad\ud800name",
)


def _state(user_id: UUID, workspace_id: UUID) -> tuple:
    with Session(engine) as session:
        workspace = session.get(Workspace, workspace_id)
        invitations = tuple(
            session.execute(
                sa.select(
                    WorkspaceInvitation.id,
                    WorkspaceInvitation.status,
                    WorkspaceInvitation.token_hash,
                ).where(WorkspaceInvitation.workspace_id == workspace_id)
            ).all()
        )
        success_audit = tuple(
            session.execute(
                sa.select(WorkspaceAuditEvent.id, WorkspaceAuditEvent.action).where(
                    WorkspaceAuditEvent.workspace_id == workspace_id,
                    WorkspaceAuditEvent.outcome == "success",
                    WorkspaceAuditEvent.action.in_(
                        ("workspace.member.invite", "workspace.settings.update")
                    ),
                )
            ).all()
        )
        usage = get_workspace_usage(
            session, user_id=user_id, workspace_id=workspace_id
        )
        return (
            workspace.name,
            invitations,
            success_audit,
            usage.members.active,
            usage.members.pending_invitations,
            usage.members.remaining,
        )


def _owner_client(owner_email: str) -> TestClient:
    client = TestClient(
        create_app(
            public_repository=EmptyPublicRepository(),
            session_factory=SessionLocal,
        )
    )
    page = client.get("/login")
    response = client.post(
        "/login",
        data={
            "csrf": _csrf(page.text),
            "email": owner_email,
            "password": PASSWORD,
            "return_to": "/app",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client


def _json_body(payload: dict) -> bytes:
    # JSON escapes a surrogate on the wire; httpx's json= path cannot encode one.
    return json.dumps(payload, ensure_ascii=True).encode("utf-8")


@pytest.mark.parametrize("email", BAD_EMAILS)
def test_normalize_email_rejects_unsafe_raw_codepoints(email: str):
    with pytest.raises(ValueError, match="invalid email"):
        normalize_email(email)


def test_normalize_email_preserves_valid_identity_contract():
    assert normalize_email("  USER@Example.COM  ") == "user@example.com"
    assert normalize_email("a" * 313 + "@x.test") == "a" * 313 + "@x.test"
    with pytest.raises(ValueError):
        normalize_email("a" * 314 + "@x.test")


def test_invalid_invitation_email_service_and_html_api_have_no_side_effects():
    owner_email = f"owner-{uuid4()}@example.test"
    owner_id, workspace_id = _bootstrap(owner_email, "Email validation", member_limit=2)
    try:
        role_id = _role_id(workspace_id, "MEMBER")
        before = _state(owner_id, workspace_id)
        for email in BAD_EMAILS:
            with Session(engine) as session:
                with pytest.raises(ActionDenied) as denied:
                    create_invitation(
                        session,
                        user_id=owner_id,
                        workspace_id=workspace_id,
                        email=email,
                        role_id=role_id,
                    )
                assert denied.value.code == "invalid_email", repr(email)
                assert denied.value.status_code == 400
                session.rollback()
            assert _state(owner_id, workspace_id) == before, repr(email)

        with _owner_client(owner_email) as client:
            csrf = client.get("/app/api/csrf").json()["csrf_token"]
            for email in BAD_EMAILS:
                # A lone surrogate cannot be encoded into an HTML form by httpx.
                if "\ud800" not in email:
                    html = client.post(
                        "/app/users/invitations",
                        data={"csrf": csrf, "email": email, "role_id": str(role_id)},
                    )
                    assert html.status_code == 400, repr(email)
                    assert "Укажите корректный email." in html.text
                    assert "psycopg" not in html.text.lower()
                    assert _state(owner_id, workspace_id) == before, repr(email)

                api = client.post(
                    "/app/api/invitations",
                    headers={
                        "x-csrf-token": csrf,
                        "content-type": "application/json",
                    },
                    content=_json_body({"email": email, "role_id": str(role_id)}),
                )
                assert api.status_code == 400, repr(email)
                assert api.json() == {
                    "error": {"code": "invalid_email", "message": "Укажите корректный email."}
                }
                assert "invite_url" not in api.text
                assert _state(owner_id, workspace_id) == before, repr(email)

            valid = client.post(
                "/app/api/invitations",
                headers={"x-csrf-token": csrf},
                json={
                    "email": "  USER@Example.COM  ",
                    "role_id": str(role_id),
                },
            )
            assert valid.status_code == 201
            assert valid.json()["invitation"]["email"] == "user@example.com"
            after = _state(owner_id, workspace_id)
            assert after[3] == before[3]
            assert after[4] == before[4] + 1
            assert after[5] == before[5] - 1
    finally:
        _purge((workspace_id,), (owner_email,))


def test_invalid_login_email_is_safe_and_valid_account_still_authenticates():
    owner_email = f"owner-{uuid4()}@example.test"
    owner_id, workspace_id = _bootstrap(owner_email, "Login validation")
    try:
        with Session(engine) as session:
            for email in BAD_EMAILS:
                attempt = authenticate_customer_attempt(session, email, PASSWORD)
                assert attempt.authenticated_user is None, repr(email)
                assert attempt.identity_ref.startswith("sha256:")
            valid = authenticate_customer_attempt(
                session, f"  {owner_email.upper()}  ", PASSWORD
            )
            assert valid.authenticated_user.id == owner_id

        app = create_app(
            public_repository=EmptyPublicRepository(), session_factory=SessionLocal
        )
        with TestClient(app) as client:
            for email in (BAD_EMAILS[0], BAD_EMAILS[5], BAD_EMAILS[-1]):
                csrf = client.get("/app/api/login/csrf").json()["csrf_token"]
                response = client.post(
                    "/app/api/login",
                    headers={
                        "x-csrf-token": csrf,
                        "content-type": "application/json",
                    },
                    content=_json_body({"email": email, "password": PASSWORD}),
                )
                assert response.status_code == 401, repr(email)
                assert response.json()["error"]["code"] == "invalid_credentials"
    finally:
        _purge((workspace_id,), (owner_email,))


def test_invalid_workspace_name_service_and_html_api_have_no_side_effects():
    owner_email = f"owner-{uuid4()}@example.test"
    owner_id, workspace_id = _bootstrap(owner_email, "Original name")
    try:
        before = _state(owner_id, workspace_id)
        for name in BAD_NAMES:
            with Session(engine) as session:
                with pytest.raises(ActionDenied) as denied:
                    update_workspace_name(
                        session,
                        user_id=owner_id,
                        workspace_id=workspace_id,
                        name=name,
                    )
                assert denied.value.code == "workspace_name_invalid_character", repr(name)
                assert denied.value.status_code == 400
                session.rollback()
            assert _state(owner_id, workspace_id) == before, repr(name)

        with _owner_client(owner_email) as client:
            csrf = client.get("/app/api/csrf").json()["csrf_token"]
            for name in BAD_NAMES:
                if "\ud800" not in name:
                    html = client.post(
                        "/app/settings/workspace",
                        data={"csrf": csrf, "name": name},
                    )
                    assert html.status_code == 400, repr(name)
                    assert "Название содержит недопустимые символы." in html.text
                    assert "psycopg" not in html.text.lower()
                    assert _state(owner_id, workspace_id) == before, repr(name)

                api = client.patch(
                    "/app/api/settings/workspace",
                    headers={
                        "x-csrf-token": csrf,
                        "content-type": "application/json",
                    },
                    content=_json_body({"name": name}),
                )
                assert api.status_code == 400, repr(name)
                assert api.json() == {
                    "error": {
                        "code": "workspace_name_invalid_character",
                        "message": "Название содержит недопустимые символы.",
                    }
                }
                assert _state(owner_id, workspace_id) == before, repr(name)

            for name, code in (
                ("   ", "workspace_name_required"),
                ("a" * 251, "workspace_name_too_long"),
            ):
                response = client.patch(
                    "/app/api/settings/workspace",
                    headers={"x-csrf-token": csrf},
                    json={"name": name},
                )
                assert response.status_code == 400
                assert response.json()["error"]["code"] == code
                assert _state(owner_id, workspace_id) == before

            valid = client.patch(
                "/app/api/settings/workspace",
                headers={"x-csrf-token": csrf},
                json={"name": "  Renamed   Workspace  "},
            )
            assert valid.status_code == 200
            assert valid.json()["workspace"]["name"] == "Renamed Workspace"
            assert _state(owner_id, workspace_id)[0] == "Renamed Workspace"
    finally:
        _purge((workspace_id,), (owner_email,))


def test_invitation_and_settings_csrf_and_member_authority_remain_enforced():
    owner_email = f"owner-{uuid4()}@example.test"
    member_email = f"member-{uuid4()}@example.test"
    owner_id, workspace_id = _bootstrap(owner_email, "Authority validation")
    _member_id, _membership_id = _add_user_membership(workspace_id, member_email)
    try:
        before = _state(owner_id, workspace_id)
        role_id = str(_role_id(workspace_id, "MEMBER"))
        with _owner_client(owner_email) as client:
            for url, data in (
                ("/app/users/invitations", {"email": "new@example.test", "role_id": role_id}),
                ("/app/settings/workspace", {"name": "New name"}),
            ):
                assert client.post(url, data=data).status_code == 403
            for method, url, payload in (
                ("post", "/app/api/invitations", {"email": "new@example.test", "role_id": role_id}),
                ("patch", "/app/api/settings/workspace", {"name": "New name"}),
            ):
                response = getattr(client, method)(url, json=payload)
                assert response.status_code == 403
                assert response.json()["error"]["code"] == "csrf_invalid"
        assert _state(owner_id, workspace_id) == before

        with _owner_client(member_email) as client:
            csrf = client.get("/app/api/csrf").json()["csrf_token"]
            rename = client.patch(
                "/app/api/settings/workspace",
                headers={"x-csrf-token": csrf},
                json={"name": "New name"},
            )
            assert rename.status_code == 403
            assert rename.json()["error"]["code"] == "permission_denied"
            invite = client.post(
                "/app/api/invitations",
                headers={"x-csrf-token": csrf},
                json={"email": "new@example.test", "role_id": role_id},
            )
            assert invite.status_code == 403
            assert invite.json()["error"]["code"] == "permission_denied"
        assert _state(owner_id, workspace_id) == before
    finally:
        _purge((workspace_id,), (owner_email, member_email))


def test_concurrent_valid_invites_reserve_only_one_available_seat():
    owner_email = f"owner-{uuid4()}@example.test"
    invite_emails = (
        f"invite-a-{uuid4()}@example.test",
        f"invite-b-{uuid4()}@example.test",
    )
    owner_id, workspace_id = _bootstrap(owner_email, "Concurrent quota", member_limit=2)
    role_id = _role_id(workspace_id, "MEMBER")
    barrier = Barrier(2)

    def invite(email: str) -> str:
        with Session(engine) as session:
            barrier.wait()
            try:
                create_invitation(
                    session,
                    user_id=owner_id,
                    workspace_id=workspace_id,
                    email=email,
                    role_id=role_id,
                )
                session.commit()
                return "success"
            except ActionDenied as exc:
                session.rollback()
                return exc.code

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = sorted(pool.map(invite, invite_emails))
        assert outcomes == ["member_quota_exceeded", "success"]
        state = _state(owner_id, workspace_id)
        assert len(state[1]) == 1
        assert state[4] == 1
        assert state[5] == 0
    finally:
        _purge((workspace_id,), (owner_email,))

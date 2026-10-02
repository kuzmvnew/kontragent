from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.postgres import SessionLocal, engine
from app.models.company import Company
from app.models.workspace import (
    CustomerSession,
    CustomerUser,
    SavedCompany,
    Workspace,
    WorkspaceAuditEvent,
    WorkspaceEntitlement,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceRoleCapability,
)
from tests.public_test_support import projection
from workspace_app.auth import (
    CSRF_COOKIE,
    create_customer_session,
    hash_password,
    load_principal,
    normalize_email,
    revoke_session,
    verify_password,
)
from workspace_app.main import create_app
from workspace_app.service import (
    ActionDenied,
    authorize,
    bootstrap_workspace_owner,
    save_company,
    saved_company_by_id,
    saved_companies,
)


PASSWORD = "correct-horse-battery-staple"


class FakePublicRepository:
    def __init__(self, items):
        self.items = {item.company.inn: item for item in items}

    def get_company(self, inn):
        return self.items.get(inn)

    def search(self, query, limit=20):
        normalized = str(query or "").casefold()
        values = [
            item
            for item in self.items.values()
            if normalized in item.company.name.casefold() or query == item.company.inn
        ]
        return values[:limit]


def _csrf_from_html(text: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', text)
    assert match, text
    return match.group(1)


def _cleanup(*emails: str, inns: tuple[str, ...] = ()) -> None:
    with Session(engine) as session:
        user_ids = tuple(
            session.scalars(
                sa.select(CustomerUser.id).where(CustomerUser.email.in_(emails))
            ).all()
        )
        workspace_ids = tuple(
            session.scalars(
                sa.select(WorkspaceMembership.workspace_id).where(
                    WorkspaceMembership.user_id.in_(user_ids)
                )
            ).all()
        ) if user_ids else ()
        if workspace_ids:
            session.execute(
                sa.delete(WorkspaceAuditEvent).where(
                    WorkspaceAuditEvent.workspace_id.in_(workspace_ids)
                )
            )
            session.execute(
                sa.delete(SavedCompany).where(SavedCompany.workspace_id.in_(workspace_ids))
            )
            session.execute(
                sa.delete(CustomerSession).where(
                    sa.or_(
                        CustomerSession.user_id.in_(user_ids),
                        CustomerSession.active_workspace_id.in_(workspace_ids),
                    )
                )
            )
            role_ids = tuple(
                session.scalars(
                    sa.select(WorkspaceRole.id).where(
                        WorkspaceRole.workspace_id.in_(workspace_ids)
                    )
                ).all()
            )
            if role_ids:
                session.execute(
                    sa.delete(WorkspaceRoleCapability).where(
                        WorkspaceRoleCapability.role_id.in_(role_ids)
                    )
                )
            session.execute(
                sa.delete(WorkspaceEntitlement).where(
                    WorkspaceEntitlement.workspace_id.in_(workspace_ids)
                )
            )
            session.execute(
                sa.delete(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id.in_(workspace_ids)
                )
            )
            session.execute(
                sa.delete(WorkspaceRole).where(
                    WorkspaceRole.workspace_id.in_(workspace_ids)
                )
            )
            session.execute(
                sa.delete(Workspace).where(Workspace.id.in_(workspace_ids))
            )
        if user_ids:
            session.execute(sa.delete(CustomerUser).where(CustomerUser.id.in_(user_ids)))
        if inns:
            session.execute(sa.delete(Company).where(Company.inn.in_(inns)))
        session.commit()


def _bootstrap(email: str, name: str, *, saved_limit: int = 3):
    with Session(engine) as session:
        user, workspace = bootstrap_workspace_owner(
            session,
            email=email,
            password_hash=hash_password(PASSWORD),
            workspace_name=name,
            saved_company_limit=saved_limit,
        )
        user_id = user.id
        workspace_id = workspace.id
        session.commit()
    return user_id, workspace_id


def _login(client: TestClient, email: str):
    page = client.get("/login")
    assert page.status_code == 200
    csrf = _csrf_from_html(page.text)
    response = client.post(
        "/login",
        data={
            "csrf": csrf,
            "email": email,
            "password": PASSWORD,
            "return_to": "/app",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] in {"/app", "/workspace/select"}
    return response


def _api_login(client: TestClient, email: str, *, password: str = PASSWORD):
    csrf_response = client.get("/app/api/login/csrf")
    assert csrf_response.status_code == 200
    csrf = csrf_response.json()["csrf_token"]
    return client.post(
        "/app/api/login",
        headers={"x-csrf-token": csrf},
        json={"email": email, "password": password},
    )


def test_workspace_p0_search_card_save_saved_and_no_internal_leakage():
    email = f"workspace-p0-{uuid4()}@example.test"
    p1 = projection(sequence=100_100_101)
    p2 = projection(sequence=100_100_102)
    inns = (p1.company.inn, p2.company.inn)
    try:
        with Session(engine) as session:
            session.add_all(
                (
                    Company(
                        inn=p1.company.inn,
                        name=p1.company.name,
                        entity_type="legal",
                    ),
                    Company(
                        inn=p2.company.inn,
                        name=p2.company.name,
                        entity_type="legal",
                    ),
                )
            )
            session.commit()
        _user_id, _workspace_id = _bootstrap(email, "Workspace P0", saved_limit=1)
        web = TestClient(
            create_app(
                public_repository=FakePublicRepository((p1, p2)),
                session_factory=SessionLocal,
            )
        )
        _login(web, email)

        home = web.get("/app")
        assert home.status_code == 200
        assert "Workspace P0" in home.text
        assert home.headers["x-robots-tag"] == "noindex, nofollow, nosnippet"
        assert home.headers["cache-control"] == "no-store"

        search = web.get(f"/app/search?q={p1.company.inn}")
        assert search.status_code == 200
        assert p1.company.name in search.text

        card = web.get(f"/app/companies/{p1.company.inn}")
        api = web.get(f"/api/app/companies/{p1.company.inn}")
        assert card.status_code == api.status_code == 200
        assert p1.company.inn in card.text
        payload = api.json()
        assert payload["company"]["inn"] == p1.company.inn
        assert payload["actions"]["is_saved"] is False
        assert payload["actions"]["can_save"] is True

        serialized = api.text + card.text
        for forbidden in (
            "company_id",
            "raw_payload",
            "parser_version",
            "worker_job_id",
            "FIRMOTEKA_AUTHORIZED_BRIDGE",
        ):
            assert forbidden not in serialized

        csrf = web.cookies[CSRF_COOKIE]
        save = web.post(
            f"/app/companies/{p1.company.inn}/save",
            data={"csrf": csrf},
            follow_redirects=False,
        )
        assert save.status_code == 303

        saved = web.get("/app/saved")
        assert saved.status_code == 200
        assert p1.company.name in saved.text
        saved_api = web.get("/api/app/saved").json()
        assert saved_api["items"][0]["inn"] == p1.company.inn
        assert "company_id" not in str(saved_api)

        card_after = web.get(f"/api/app/companies/{p1.company.inn}").json()
        assert card_after["actions"]["is_saved"] is True

        quota = web.post(
            f"/app/companies/{p2.company.inn}/save",
            data={"csrf": csrf},
        )
        assert quota.status_code == 403
        assert "quota_exceeded" in quota.text
        assert p2.company.inn not in web.get("/api/app/saved").text

        with Session(engine) as session:
            outcomes = tuple(
                session.scalars(
                    sa.select(WorkspaceAuditEvent.outcome).where(
                        WorkspaceAuditEvent.action == "company.save"
                    )
                ).all()
            )
            assert "success" in outcomes
            assert "quota_exceeded" in outcomes
    finally:
        _cleanup(email, inns=inns)


def test_workspace_p0_tenant_isolation_and_workspace_selection_fail_closed():
    email_a = f"tenant-a-{uuid4()}@example.test"
    email_b = f"tenant-b-{uuid4()}@example.test"
    p = projection(sequence=100_100_103)
    try:
        with Session(engine) as session:
            session.add(
                Company(inn=p.company.inn, name=p.company.name, entity_type="legal")
            )
            session.commit()
        user_a, workspace_a = _bootstrap(email_a, "Tenant A")
        user_b, workspace_b = _bootstrap(email_b, "Tenant B")

        with Session(engine) as session:
            save_entitlement = session.scalar(
                sa.select(WorkspaceEntitlement).where(
                    WorkspaceEntitlement.workspace_id == workspace_a,
                    WorkspaceEntitlement.entitlement_key == "saved_companies.enabled",
                )
            )
            assert save_entitlement is not None
            assert workspace_a != workspace_b
            authorize(
                session,
                user_id=user_a,
                workspace_id=workspace_a,
                permission_key="company.view",
            )
            try:
                authorize(
                    session,
                    user_id=user_a,
                    workspace_id=workspace_b,
                    permission_key="company.view",
                )
            except ActionDenied as exc:
                assert exc.code == "membership_required"
            else:
                raise AssertionError("tenant A accessed tenant B")

        web_a = TestClient(
            create_app(
                public_repository=FakePublicRepository((p,)),
                session_factory=SessionLocal,
            )
        )
        _login(web_a, email_a)
        csrf = web_a.cookies[CSRF_COOKIE]
        cross = web_a.post(
            "/workspace/select",
            data={"csrf": csrf, "workspace_id": str(workspace_b)},
        )
        assert cross.status_code == 403
        assert "workspace_selection_denied" in cross.text

        own = web_a.get(f"/api/app/companies/{p.company.inn}")
        assert own.status_code == 200

        web_b = TestClient(
            create_app(
                public_repository=FakePublicRepository((p,)),
                session_factory=SessionLocal,
            )
        )
        _login(web_b, email_b)
        assert web_b.get("/api/app/saved").json()["items"] == []
    finally:
        _cleanup(email_a, email_b, inns=(p.company.inn,))


def test_workspace_p0_permission_entitlement_quota_and_csrf_are_distinct():
    email = f"workspace-gates-{uuid4()}@example.test"
    p = projection(sequence=100_100_104)
    try:
        with Session(engine) as session:
            session.add(
                Company(inn=p.company.inn, name=p.company.name, entity_type="legal")
            )
            session.commit()
        user_id, workspace_id = _bootstrap(email, "Gates", saved_limit=1)
        web = TestClient(
            create_app(
                public_repository=FakePublicRepository((p,)),
                session_factory=SessionLocal,
            )
        )
        _login(web, email)

        bad_csrf = web.post(
            f"/app/companies/{p.company.inn}/save",
            data={"csrf": "wrong"},
        )
        assert bad_csrf.status_code == 403
        assert "csrf_invalid" in bad_csrf.text

        with Session(engine) as session:
            membership = session.scalar(
                sa.select(WorkspaceMembership).where(
                    WorkspaceMembership.user_id == user_id,
                    WorkspaceMembership.workspace_id == workspace_id,
                )
            )
            assert membership is not None
            session.execute(
                sa.delete(WorkspaceRoleCapability).where(
                    WorkspaceRoleCapability.role_id == membership.role_id,
                    WorkspaceRoleCapability.capability_key == "company.save",
                )
            )
            session.commit()

        permission_card = web.get(f"/api/app/companies/{p.company.inn}").json()
        assert permission_card["actions"]["can_save"] is False
        assert permission_card["actions"]["save_denial_reason"] == "permission_denied"

        with Session(engine) as session:
            membership = session.scalar(
                sa.select(WorkspaceMembership).where(
                    WorkspaceMembership.user_id == user_id,
                    WorkspaceMembership.workspace_id == workspace_id,
                )
            )
            session.add(
                WorkspaceRoleCapability(
                    role_id=membership.role_id,
                    capability_key="company.save",
                )
            )
            entitlement = session.scalar(
                sa.select(WorkspaceEntitlement).where(
                    WorkspaceEntitlement.workspace_id == workspace_id,
                    WorkspaceEntitlement.entitlement_key == "saved_companies.enabled",
                )
            )
            entitlement.enabled = False
            session.commit()

        entitlement_card = web.get(f"/api/app/companies/{p.company.inn}").json()
        assert entitlement_card["actions"]["save_denial_reason"] == "entitlement_blocked"

        with Session(engine) as session:
            entitlement = session.scalar(
                sa.select(WorkspaceEntitlement).where(
                    WorkspaceEntitlement.workspace_id == workspace_id,
                    WorkspaceEntitlement.entitlement_key == "saved_companies.enabled",
                )
            )
            entitlement.enabled = True
            entitlement.limit_value = 0
            session.commit()

        quota_card = web.get(f"/api/app/companies/{p.company.inn}").json()
        assert quota_card["actions"]["can_save"] is False
        assert quota_card["actions"]["save_denial_reason"] == "quota_exceeded"
    finally:
        _cleanup(email, inns=(p.company.inn,))


def test_workspace_p0_session_revoke_blocks_private_routes():
    email = f"workspace-session-{uuid4()}@example.test"
    try:
        _bootstrap(email, "Session")
        web = TestClient(
            create_app(
                public_repository=FakePublicRepository(()),
                session_factory=SessionLocal,
            )
        )
        _login(web, email)
        assert web.get("/app").status_code == 200
        csrf = web.cookies[CSRF_COOKIE]
        logout = web.post("/logout", data={"csrf": csrf}, follow_redirects=False)
        assert logout.status_code == 303
        assert logout.headers["location"] == "/login"
        private = web.get("/app", follow_redirects=False)
        assert private.status_code == 303
        assert private.headers["location"].startswith("/login")
    finally:
        _cleanup(email)


def test_workspace_p0_canonical_api_login_save_list_unsave_logout():
    email = f"workspace-api-{uuid4()}@example.test"
    p = projection(sequence=100_100_105)
    try:
        with Session(engine) as session:
            session.add(Company(inn=p.company.inn, name=p.company.name, entity_type="legal"))
            session.commit()
        _bootstrap(email, "Canonical API", saved_limit=2)
        web = TestClient(
            create_app(
                public_repository=FakePublicRepository((p,)),
                session_factory=SessionLocal,
            )
        )

        anonymous = web.get("/app/api/context")
        assert anonymous.status_code == 401
        assert anonymous.json()["error"]["code"] == "authentication_required"

        csrf_page = web.get("/app/api/login/csrf")
        no_csrf = web.post(
            "/app/api/login",
            json={"email": email, "password": PASSWORD},
        )
        assert csrf_page.status_code == 200
        assert no_csrf.status_code == 403

        wrong = _api_login(web, email, password="not-the-password")
        assert wrong.status_code == 401
        assert PASSWORD not in wrong.text
        assert "password_hash" not in wrong.text

        login = _api_login(web, email)
        assert login.status_code == 200
        session_cookie = "\n".join(login.headers.get_list("set-cookie"))
        assert "HttpOnly" in session_cookie
        assert "SameSite=strict" in session_cookie

        context = web.get("/app/api/context")
        assert context.status_code == 200
        assert context.json()["user"] == {"email": email}
        assert context.json()["workspace"] == {"name": "Canonical API", "role": "OWNER"}
        for forbidden in ("password", "password_hash", "user_id", "workspace_id", "company_id"):
            assert forbidden not in context.text

        card = web.get(f"/app/api/companies/{p.company.inn}")
        assert card.status_code == 200
        assert card.json()["company"]["inn"] == p.company.inn
        assert card.json()["actions"]["is_saved"] is False
        assert "company_id" not in card.text

        rejected = web.post(f"/app/api/companies/{p.company.inn}/saved")
        assert rejected.status_code == 403
        assert rejected.json()["error"]["code"] == "csrf_invalid"

        csrf = web.get("/app/api/csrf").json()["csrf_token"]
        saved = web.post(
            f"/app/api/companies/{p.company.inn}/saved",
            headers={"x-csrf-token": csrf},
        )
        duplicate = web.post(
            f"/app/api/companies/{p.company.inn}/saved",
            headers={"x-csrf-token": csrf},
        )
        assert saved.status_code == 201
        assert saved.json() == {"saved": True, "created": True}
        assert duplicate.status_code == 200
        assert duplicate.json() == {"saved": True, "created": False}

        listing = web.get("/app/api/saved-companies")
        assert listing.status_code == 200
        assert [item["inn"] for item in listing.json()["items"]] == [p.company.inn]
        assert "company_id" not in listing.text

        removed = web.delete(
            f"/app/api/companies/{p.company.inn}/saved",
            headers={"x-csrf-token": csrf},
        )
        absent = web.delete(
            f"/app/api/companies/{p.company.inn}/saved",
            headers={"x-csrf-token": csrf},
        )
        assert removed.json() == {"saved": False, "removed": True}
        assert absent.json() == {"saved": False, "removed": False}

        logout = web.post("/app/api/logout", headers={"x-csrf-token": csrf})
        assert logout.status_code == 204
        assert web.get("/app/api/context").status_code == 401

        with Session(engine) as session:
            outcomes = tuple(
                session.scalars(
                    sa.select(WorkspaceAuditEvent.outcome).where(
                        WorkspaceAuditEvent.action.in_(("company.save", "company.unsave")),
                        WorkspaceAuditEvent.workspace_id.in_(
                            sa.select(WorkspaceMembership.workspace_id).where(
                                WorkspaceMembership.user_id.in_(
                                    sa.select(CustomerUser.id).where(CustomerUser.email == email)
                                )
                            )
                        ),
                    )
                ).all()
            )
        assert {"success", "already_saved", "not_saved"}.issubset(outcomes)
    finally:
        _cleanup(email, inns=(p.company.inn,))


def test_workspace_p0_session_expiry_tamper_revocation_and_disabled_user():
    email = f"workspace-session-security-{uuid4()}@example.test"
    try:
        user_id, workspace_id = _bootstrap(email, "Session Security")
        now = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
        with Session(engine) as session:
            user = session.get(CustomerUser, user_id)
            token, _csrf, _record = create_customer_session(
                session,
                user=user,
                active_workspace_id=workspace_id,
                now=now,
                ttl=timedelta(minutes=5),
            )
            session.commit()

        with Session(engine) as session:
            principal = load_principal(session, token, now=now + timedelta(minutes=1))
            assert principal is not None
            assert load_principal(session, token + "tampered", now=now + timedelta(minutes=1)) is None
            assert load_principal(session, token, now=now + timedelta(minutes=5)) is None
            revoke_session(session, principal, now=now + timedelta(minutes=2))
            session.commit()

        with Session(engine) as session:
            assert load_principal(session, token, now=now + timedelta(minutes=3)) is None
            user = session.get(CustomerUser, user_id)
            token2, _csrf2, _record2 = create_customer_session(
                session,
                user=user,
                active_workspace_id=workspace_id,
                now=now,
            )
            user.status = "disabled"
            session.commit()

        with Session(engine) as session:
            assert load_principal(session, token2, now=now + timedelta(minutes=1)) is None
    finally:
        _cleanup(email)


def test_workspace_p0_membership_user_permission_and_entitlement_fail_closed():
    email = f"workspace-fail-closed-{uuid4()}@example.test"
    try:
        user_id, workspace_id = _bootstrap(email, "Fail Closed")
        with Session(engine) as session:
            membership = session.scalar(
                sa.select(WorkspaceMembership).where(
                    WorkspaceMembership.user_id == user_id,
                    WorkspaceMembership.workspace_id == workspace_id,
                )
            )
            membership.status = "suspended"
            session.commit()
        with Session(engine) as session:
            try:
                authorize(
                    session,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    permission_key="company.view",
                )
            except ActionDenied as exc:
                assert exc.code == "membership_required"
            else:
                raise AssertionError("disabled membership authorized")

            membership = session.scalar(
                sa.select(WorkspaceMembership).where(
                    WorkspaceMembership.user_id == user_id,
                    WorkspaceMembership.workspace_id == workspace_id,
                )
            )
            membership.status = "active"
            session.get(CustomerUser, user_id).status = "disabled"
            session.commit()
        with Session(engine) as session:
            try:
                authorize(
                    session,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    permission_key="company.view",
                )
            except ActionDenied as exc:
                assert exc.code == "authentication_required"
            else:
                raise AssertionError("disabled user authorized")
    finally:
        _cleanup(email)


def test_workspace_p0_resource_id_is_scoped_and_company_identity_is_global():
    email_a = f"resource-a-{uuid4()}@example.test"
    email_b = f"resource-b-{uuid4()}@example.test"
    p = projection(sequence=100_100_106)
    try:
        with Session(engine) as session:
            session.add(Company(inn=p.company.inn, name=p.company.name, entity_type="legal"))
            session.commit()
        user_a, workspace_a = _bootstrap(email_a, "Resource A")
        user_b, workspace_b = _bootstrap(email_b, "Resource B")

        with Session(engine) as session:
            assert save_company(
                session,
                user_id=user_a,
                workspace_id=workspace_a,
                inn=p.company.inn,
            )
            session.commit()
            saved = session.scalar(
                sa.select(SavedCompany).where(SavedCompany.workspace_id == workspace_a)
            )
            saved_id = saved.id
            company_id = saved.company_id

        with Session(engine) as session:
            assert session.scalar(sa.select(sa.func.count()).select_from(Company).where(Company.id == company_id)) == 1
            try:
                saved_company_by_id(
                    session,
                    user_id=user_b,
                    workspace_id=workspace_b,
                    saved_company_id=saved_id,
                )
            except ActionDenied as exc:
                assert exc.code == "saved_company_not_found"
                assert exc.status_code == 404
            else:
                raise AssertionError("resource id bypassed workspace scope")

            try:
                authorize(
                    session,
                    user_id=user_a,
                    workspace_id=workspace_b,
                    permission_key="company.view",
                )
            except ActionDenied as exc:
                assert exc.code == "membership_required"
            else:
                raise AssertionError("workspace A membership authorized workspace B")
    finally:
        _cleanup(email_a, email_b, inns=(p.company.inn,))


def test_workspace_p0_database_constraints_and_role_contract():
    email_a = f"constraints-a-{uuid4()}@example.test"
    email_b = f"constraints-b-{uuid4()}@example.test"
    try:
        user_a, workspace_a = _bootstrap(email_a, "Constraints A")
        _user_b, workspace_b = _bootstrap(email_b, "Constraints B")

        with Session(engine) as session:
            role_keys = set(
                session.scalars(
                    sa.select(WorkspaceRole.role_key).where(
                        WorkspaceRole.workspace_id == workspace_a
                    )
                ).all()
            )
            entitlement_keys = set(
                session.scalars(
                    sa.select(WorkspaceEntitlement.entitlement_key).where(
                        WorkspaceEntitlement.workspace_id == workspace_a
                    )
                ).all()
            )
            assert role_keys == {"OWNER", "ADMIN", "MEMBER"}
            assert entitlement_keys == {
                "workspace.core.enabled",
                "saved_companies.enabled",
                "monitoring.enabled",
                "workspace_members.enabled",
            }
            assert entitlement_keys.isdisjoint(
                {
                    "workspace.view",
                    "company.search",
                    "company.view",
                    "company.save",
                    "company.unsave",
                    "monitoring.manage",
                    "workspace.members.manage",
                }
            )

            member_role_a = session.scalar(
                sa.select(WorkspaceRole).where(
                    WorkspaceRole.workspace_id == workspace_a,
                    WorkspaceRole.role_key == "MEMBER",
                )
            )
            session.add(
                WorkspaceMembership(
                    workspace_id=workspace_a,
                    user_id=user_a,
                    role_id=member_role_a.id,
                    status="active",
                )
            )
            try:
                session.flush()
            except IntegrityError:
                session.rollback()
            else:
                raise AssertionError("duplicate membership was accepted")

        with Session(engine) as session:
            member_role_a = session.scalar(
                sa.select(WorkspaceRole).where(
                    WorkspaceRole.workspace_id == workspace_a,
                    WorkspaceRole.role_key == "MEMBER",
                )
            )
            session.add(
                WorkspaceMembership(
                    workspace_id=workspace_b,
                    user_id=user_a,
                    role_id=member_role_a.id,
                    status="active",
                )
            )
            try:
                session.flush()
            except IntegrityError:
                session.rollback()
            else:
                raise AssertionError("cross-workspace role assignment was accepted")
    finally:
        _cleanup(email_a, email_b)


def test_workspace_p0_password_hash_and_email_normalization():
    encoded = hash_password(PASSWORD, salt=b"0123456789abcdef")
    assert encoded != PASSWORD
    assert verify_password(PASSWORD, encoded)
    assert not verify_password("incorrect-password", encoded)
    assert not verify_password(PASSWORD, "not-a-valid-hash")
    assert normalize_email("  Owner@Example.TEST  ") == "owner@example.test"


def test_workspace_p0_non_local_cookie_is_secure(monkeypatch):
    email = f"workspace-secure-cookie-{uuid4()}@example.test"
    try:
        _bootstrap(email, "Secure Cookie")
        monkeypatch.setenv("WORKSPACE_ENV", "production")
        web = TestClient(
            create_app(
                public_repository=FakePublicRepository(()),
                session_factory=SessionLocal,
            ),
            base_url="https://testserver",
        )
        response = _api_login(web, email)
        assert response.status_code == 200
        cookies = "\n".join(response.headers.get_list("set-cookie"))
        assert "Secure" in cookies
        assert "HttpOnly" in cookies
        assert "SameSite=strict" in cookies
    finally:
        _cleanup(email)

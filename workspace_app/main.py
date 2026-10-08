"""NEXT Company customer Workspace P0 FastAPI application."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode, urlsplit
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.database.postgres import SessionLocal
from app.models.workspace import Workspace, WorkspaceRole
from public_app.contracts import valid_legal_inn
from public_app.repository import PublicRepository
from workspace_app.auth import (
    CSRF_COOKIE,
    INVITE_CSRF_COOKIE,
    LOGIN_CSRF_COOKIE,
    SESSION_COOKIE,
    active_membership_workspaces,
    create_customer_session,
    load_principal,
    login_csrf_valid,
    new_login_csrf,
    revoke_session,
    safe_return_to,
    set_active_workspace,
    verify_session_csrf,
)
from workspace_app.dashboard_service import get_workspace_dashboard
from workspace_app.service import (
    ActionDenied,
    authenticate_customer_attempt,
    authorize,
    action_state,
    list_active_workspaces,
    record_login_audit,
    record_logout_audit,
    record_workspace_selection_audit,
    saved_companies,
    saved_company_for_inn,
    saved_count,
    save_company,
    unsave_company,
    update_saved_company_note,
)
from workspace_app.monitoring_service import (
    get_monitoring_state,
    list_workspace_feed,
    list_workspace_subscriptions,
    mark_feed_entry_read,
    pause_subscription,
    resume_subscription,
    subscribe_company,
)
from workspace_app.report_service import (
    canonical_json_bytes,
    generate_report,
    get_report,
    list_reports,
    report_csv_bytes,
    report_filename,
    report_generation_state,
    report_list_payload,
)
from workspace_app.bulk_service import (
    ITEM_STATUSES,
    MAX_BULK_MULTIPART_BODY_BYTES,
    MAX_FILE_BYTES,
    bulk_csv_bytes,
    bulk_filename,
    bulk_json_bytes,
    cancel_bulk_job,
    create_bulk_job,
    get_bulk_job,
    item_payload,
    job_payload,
    list_bulk_items,
    list_bulk_jobs,
    process_bulk_job_chunk,
    record_bulk_export,
    resume_bulk_job,
    retry_bulk_job,
)
from workspace_app.member_service import (
    accept_invitation,
    change_member_role,
    change_member_status,
    create_invitation,
    invitation_preview,
    list_invitations,
    list_members,
    list_system_roles,
    reissue_invitation,
    revoke_invitation,
)
from workspace_app.settings_service import (
    get_workspace_settings,
    update_workspace_name,
)


ROOT = Path(__file__).resolve().parent


def _active_nav(path: str) -> str:
    if path == "/app":
        return "home"
    if path == "/app/saved" or path.startswith("/app/saved/"):
        return "saved"
    if path == "/app/monitoring" or (
        path.startswith("/app/companies/") and "/monitoring" in path
    ):
        return "monitoring"
    if path == "/app/reports" or path.startswith("/app/reports/"):
        return "reports"
    if path == "/app/bulk" or path.startswith("/app/bulk/"):
        return "bulk"
    if path == "/app/users" or path.startswith("/app/users/"):
        return "users"
    if path == "/app/settings" or path.startswith("/app/settings/"):
        return "settings"
    if path == "/app/search" or path.startswith("/app/companies/"):
        return "search"
    return ""


def _shared_template_context(request: Request) -> dict:
    return {
        "public_origin": request.app.state.public_origin,
        "active_nav": _active_nav(request.url.path),
        "demo_mode": request.app.state.demo_mode,
    }


templates = Jinja2Templates(
    directory=str(ROOT / "templates"),
    context_processors=[_shared_template_context],
)
MAX_BODY_BYTES = 32_768
BULK_CREATE_PATHS = frozenset(("/app/bulk", "/app/api/bulk-jobs"))


class _RequestBodyLimitExceeded(Exception):
    pass


class RequestBodyLimitMiddleware:
    """Enforce route-aware limits on Content-Length and the actual ASGI stream."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_bulk_create = scope.get("method") == "POST" and scope.get("path") in BULK_CREATE_PATHS
        limit = MAX_BULK_MULTIPART_BODY_BYTES if is_bulk_create else MAX_BODY_BYTES
        length_values = [
            value for key, value in scope.get("headers", []) if key.lower() == b"content-length"
        ]
        if len(length_values) > 1:
            await self._error(scope, receive, send, 400, "invalid_request", "Invalid request")
            return
        if length_values:
            raw_length = length_values[0]
            if not raw_length.isdigit():
                await self._error(scope, receive, send, 400, "invalid_request", "Invalid request")
                return
            if int(raw_length) > limit:
                code = "bulk_request_too_large" if is_bulk_create else "request_too_large"
                await self._error(scope, receive, send, 413, code, "Request too large")
                return

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _RequestBodyLimitExceeded
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _RequestBodyLimitExceeded:
            if response_started:
                raise
            code = "bulk_request_too_large" if is_bulk_create else "request_too_large"
            await self._error(scope, receive, send, 413, code, "Request too large")

    @staticmethod
    async def _error(
        scope: Scope,
        receive: Receive,
        send: Send,
        status_code: int,
        code: str,
        message: str,
    ) -> None:
        response = JSONResponse(
            {"error": {"code": code, "message": message}},
            status_code=status_code,
        )
        await response(scope, receive, send)


_VALUE_LABELS = {
    "amount": "Сумма",
    "count": "Количество",
    "currency": "Валюта",
    "name": "Наименование",
    "number": "Номер",
    "position": "Должность",
    "record_count": "Количество записей",
    "registry_number": "Номер в реестре",
    "result": "Результат",
    "share": "Доля",
    "status": "Статус",
    "total": "Итого",
    "value": "Значение",
}


def _workspace_value(value) -> str:
    """Render already-minimized semantic values without exposing internals."""

    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "Да" if value else "Нет"
    if isinstance(value, (date, datetime)):
        return value.strftime("%d.%m.%Y")
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, Mapping):
        parts = []
        for key, child in value.items():
            label = _VALUE_LABELS.get(str(key), str(key).replace("_", " ").capitalize())
            parts.append(f"{label}: {_workspace_value(child)}")
        return " · ".join(parts) or "—"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return "; ".join(_workspace_value(item) for item in value) or "—"
    return str(value)


templates.env.filters["workspace_value"] = _workspace_value


def _cookie_secure() -> bool:
    environment = os.getenv("WORKSPACE_ENV", "local").strip().lower()
    if environment not in {"local", "development", "test"}:
        return True
    return os.getenv("WORKSPACE_COOKIE_SECURE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _validated_public_origin(value: str | None) -> str:
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return ""
    parsed = urlsplit(raw)
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("PUBLIC_ORIGIN contains an invalid port") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("PUBLIC_ORIGIN must be an HTTP(S) origin without a path")
    return f"{parsed.scheme}://{parsed.netloc}"


def _session_factory(request: Request):
    return request.app.state.session_factory


def _public_repository(request: Request):
    return request.app.state.public_repository


def _error_payload(exc: ActionDenied) -> dict:
    return {"error": {"code": exc.code, "message": exc.message}}


def _request_uuid(value, *, field: str) -> UUID:
    try:
        return UUID(str(value or ""))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ActionDenied(
            "invalid_request",
            f"Некорректное поле {field}.",
            status_code=400,
        ) from exc


def _html_error(request: Request, exc: ActionDenied):
    return templates.TemplateResponse(
        request=request,
        name="error.html",
        context={"code": exc.code, "message": exc.message},
        status_code=exc.status_code,
    )


def _workspace_shell(session, context) -> tuple[Workspace, WorkspaceRole]:
    workspace = session.get(Workspace, context.workspace_id)
    role = session.get(WorkspaceRole, context.role_id)
    if workspace is None or role is None:
        raise ActionDenied(
            "workspace_unavailable",
            "Рабочее пространство недоступно.",
        )
    return workspace, role


def _set_session_cookies(response, *, token: str, csrf: str, secure: bool) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/",
        max_age=8 * 60 * 60,
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/",
        max_age=8 * 60 * 60,
    )


def _clear_session_cookies(response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


def _active_principal(request: Request, session):
    return load_principal(session, request.cookies.get(SESSION_COOKIE))


def _require_principal(request: Request, session):
    principal = _active_principal(request, session)
    if principal is None:
        raise ActionDenied(
            "authentication_required",
            "Войдите в аккаунт.",
            status_code=401,
        )
    return principal


def _require_active_workspace(request: Request, session, *, permission: str):
    principal = _require_principal(request, session)
    if principal.active_workspace_id is None:
        raise ActionDenied(
            "workspace_required",
            "Выберите рабочее пространство.",
            status_code=403,
        )
    context = authorize(
        session,
        user_id=principal.user_id,
        workspace_id=principal.active_workspace_id,
        permission_key=permission,
    )
    return principal, context


def _verify_post_csrf(request: Request, session, principal, form_value: str | None) -> None:
    if not verify_session_csrf(
        session,
        principal,
        cookie_value=request.cookies.get(CSRF_COOKIE),
        form_value=form_value,
    ):
        raise ActionDenied("csrf_invalid", "Запрос отклонён защитой CSRF.", status_code=403)


def _card_context(session, principal, projection) -> dict:
    workspace_id = principal.active_workspace_id
    if workspace_id is None:
        raise ActionDenied("workspace_required", "Выберите рабочее пространство.")
    authorize(
        session,
        user_id=principal.user_id,
        workspace_id=workspace_id,
        permission_key="company.view",
    )
    saved_entry = saved_company_for_inn(
        session,
        user_id=principal.user_id,
        workspace_id=workspace_id,
        inn=projection.company.inn,
    )
    saved = saved_entry is not None
    can_save, denial, limit_value = action_state(
        session,
        user_id=principal.user_id,
        workspace_id=workspace_id,
        permission_key="company.save",
    )
    used = saved_count(
        session,
        user_id=principal.user_id,
        workspace_id=workspace_id,
    )
    remaining = None if limit_value is None else max(limit_value - used, 0)
    effective_save_denial = (
        denial
        if not can_save
        else ("quota_exceeded" if remaining == 0 and not saved else None)
    )
    save_denial_message = {
        "permission_denied": "У вашей роли нет права сохранять компании.",
        "entitlement_blocked": "Сохранение компаний не подключено для этого Workspace.",
        "quota_exceeded": "Достигнут лимит сохранённых компаний.",
    }.get(effective_save_denial, "Сохранение недоступно для этого Workspace.")
    state = get_monitoring_state(
        session,
        user_id=principal.user_id,
        workspace_id=workspace_id,
        inn=projection.company.inn,
    )
    monitoring = _monitoring_payload(state)
    monitoring["entry_enabled"] = saved
    can_unsave, unsave_denial, _unsave_limit = action_state(
        session,
        user_id=principal.user_id,
        workspace_id=workspace_id,
        permission_key="company.unsave",
    )
    if state.state == "ACTIVE":
        can_unsave = False
        unsave_denial = "monitoring_active"
    unsave_denial_message = {
        "permission_denied": "У вашей роли нет права удалять сохранённые компании.",
        "entitlement_blocked": "Сохранённые компании не подключены для этого Workspace.",
        "monitoring_active": "Сначала приостановите мониторинг компании.",
    }.get(unsave_denial, "Удаление из сохранённых недоступно.")
    (
        can_generate_report,
        report_denial,
        report_limit,
        report_used,
        report_remaining,
    ) = report_generation_state(
        session,
        user_id=principal.user_id,
        workspace_id=workspace_id,
    )
    report_denial_message = {
        "permission_denied": "У вашей роли нет права формировать отчёты.",
        "entitlement_blocked": "Формирование отчётов не подключено для этого Workspace.",
        "quota_exceeded": "Достигнут лимит сохранённых отчётов.",
    }.get(report_denial, "Формирование отчёта недоступно.")
    return {
        "is_saved": saved,
        "saved_note": saved_entry.note if saved_entry is not None else None,
        "can_save": can_save and (remaining is None or remaining > 0 or saved),
        "save_denial_reason": effective_save_denial,
        "save_denial_message": save_denial_message,
        "saved_limit": limit_value,
        "saved_used": used,
        "saved_remaining": remaining,
        "can_unsave": can_unsave,
        "unsave_denial_reason": unsave_denial,
        "unsave_denial_message": unsave_denial_message,
        "monitoring": monitoring,
        "can_generate_report": can_generate_report,
        "report_denial_reason": report_denial,
        "report_denial_message": report_denial_message,
        "report_limit": report_limit,
        "report_used": report_used,
        "report_remaining": report_remaining,
    }


def _monitoring_payload(state) -> dict:
    return {
        "state": state.state,
        "message": state.message,
        "can_manage": state.can_manage,
        "denial_code": state.denial_code,
        "subscription_id": str(state.subscription_id) if state.subscription_id else None,
        "started_at": state.started_at.isoformat() if state.started_at else None,
        "paused_at": state.paused_at.isoformat() if state.paused_at else None,
        "last_checked_at": state.last_checked_at.isoformat() if state.last_checked_at else None,
    }


def _feed_payload(item) -> dict:
    return {
        "id": str(item.entry_id),
        "event_ref": item.event_ref,
        "company": {"inn": item.inn, "name": item.company_name},
        "title": item.title,
        "event_type": item.event_type,
        "change_kind": item.change_kind,
        "old_value": item.old_value,
        "new_value": item.new_value,
        "old_state": item.old_state,
        "new_state": item.new_state,
        "severity": item.severity,
        "detected_at": item.detected_at.isoformat(),
        "source_code": item.source_code,
        "evidence_refs": list(item.evidence_refs),
        "read_at": item.read_at.isoformat() if item.read_at else None,
    }


def _saved_payload(item) -> dict:
    return {
        "id": str(item.saved_company_id),
        "inn": item.inn,
        "name": item.name,
        "note": item.note,
        "created_at": item.created_at.isoformat(),
        "monitoring": {
            "state": item.monitoring_state,
            "subscription_id": (
                str(item.monitoring_subscription_id)
                if item.monitoring_subscription_id
                else None
            ),
            "last_checked_at": (
                item.monitoring_last_checked_at.isoformat()
                if item.monitoring_last_checked_at
                else None
            ),
        },
    }


def _subscription_payload(item) -> dict:
    return {
        "id": str(item.subscription_id),
        "company": {"inn": item.inn, "name": item.company_name},
        "status": item.status,
        "is_saved": item.is_saved,
        "can_pause": item.can_pause,
        "can_resume": item.can_resume,
        "resume_denial_code": item.resume_denial_code,
        "resume_denial_message": item.resume_denial_message,
        "started_at": item.started_at.isoformat(),
        "paused_at": item.paused_at.isoformat() if item.paused_at else None,
        "last_checked_at": (
            item.last_checked_at.isoformat() if item.last_checked_at else None
        ),
        "latest_event_at": (
            item.latest_event_at.isoformat() if item.latest_event_at else None
        ),
    }


def _dashboard_payload(dashboard) -> dict:
    metrics = dashboard.metrics
    return {
        "metrics": {
            "saved_companies_count": metrics.saved_companies_count,
            "saved_companies_limit": metrics.saved_companies_limit,
            "saved_companies_remaining": metrics.saved_companies_remaining,
            "active_monitoring_count": metrics.active_monitoring_count,
            "paused_monitoring_count": metrics.paused_monitoring_count,
            "unread_monitoring_event_count": metrics.unread_monitoring_event_count,
            "total_monitoring_event_count": metrics.total_monitoring_event_count,
        },
        "quota": {
            "enabled": dashboard.quota.enabled,
            "used": dashboard.quota.used,
            "limit": dashboard.quota.limit,
            "remaining": dashboard.quota.remaining,
        },
        "monitoring_access": {
            "available": dashboard.monitoring_available,
            "denial": dashboard.monitoring_denial,
        },
        "recent_saved": [_saved_payload(item) for item in dashboard.recent_saved],
        "recent_events": [_feed_payload(item) for item in dashboard.recent_events],
    }


def _member_payload(member) -> dict:
    return {
        "id": str(member.membership_id),
        "email": member.email,
        "role": {
            "id": str(member.role_id),
            "key": member.role_key,
            "name": member.role_name,
        },
        "status": member.status,
        "joined_at": member.joined_at.isoformat(),
        "current_user": member.is_current_user,
    }


def _invitation_payload(invitation) -> dict:
    return {
        "id": str(invitation.invitation_id),
        "email": invitation.email,
        "role": {
            "id": str(invitation.role_id),
            "key": invitation.role_key,
            "name": invitation.role_name,
        },
        "status": invitation.status,
        "created_at": invitation.created_at.isoformat(),
        "expires_at": invitation.expires_at.isoformat(),
    }


def _role_payload(role) -> dict:
    return {
        "id": str(role.role_id),
        "key": role.role_key,
        "name": role.role_name,
        "capabilities": list(role.capabilities),
    }


def _usage_payload(usage) -> dict:
    return {
        "workspace": {
            "status": usage.workspace_status,
            "created_at": usage.workspace_created_at.isoformat(),
        },
        "members": {
            "active": usage.members.active,
            "pending_invitations": usage.members.pending_invitations,
            "enabled": usage.members.enabled,
            "limit": usage.members.limit,
            "remaining": usage.members.remaining,
        },
        "saved": {
            "used": usage.saved.used,
            "enabled": usage.saved.enabled,
            "limit": usage.saved.limit,
            "remaining": usage.saved.remaining,
        },
        "monitoring": {
            "enabled": usage.monitoring.enabled,
            "active_subscriptions": usage.monitoring.active_subscriptions,
            "paused_subscriptions": usage.monitoring.paused_subscriptions,
        },
        "reports": {
            "used": usage.reports.used,
            "enabled": usage.reports.enabled,
            "limit": usage.reports.limit,
            "remaining": usage.reports.remaining,
        },
        "bulk": {
            "enabled": usage.bulk.enabled,
            "job_count": usage.bulk.job_count,
            "per_job_unique_inn_limit": usage.bulk.per_job_unique_inn_limit,
        },
        "entitlements": [
            {"key": item.key, "enabled": item.enabled, "limit": item.limit}
            for item in usage.entitlements
        ],
    }


def _settings_payload(settings) -> dict:
    return {
        "workspace": {
            "id": str(settings.workspace_id),
            "name": settings.workspace_name,
            "status": settings.workspace_status,
        },
        "current_user": {
            "email": settings.current_user_email,
            "role": {
                "key": settings.current_role_key,
                "name": settings.current_role_name,
            },
        },
        "usage": _usage_payload(settings.usage),
    }


def _users_template_response(
    request: Request,
    session,
    principal,
    context,
    *,
    invitation_secret=None,
    notice: str = "",
):
    workspace, role = _workspace_shell(session, context)
    members = list_members(
        session,
        user_id=principal.user_id,
        workspace_id=context.workspace_id,
    )
    invitations = list_invitations(
        session,
        user_id=principal.user_id,
        workspace_id=context.workspace_id,
    )
    roles = list_system_roles(
        session,
        user_id=principal.user_id,
        workspace_id=context.workspace_id,
    )
    can_manage, _manage_denial, _manage_limit = action_state(
        session,
        user_id=principal.user_id,
        workspace_id=context.workspace_id,
        permission_key="workspace.members.manage",
    )
    can_invite, invite_denial, _invite_limit = action_state(
        session,
        user_id=principal.user_id,
        workspace_id=context.workspace_id,
        permission_key="workspace.members.invite",
    )
    invite_url = (
        f"/invite/{invitation_secret.token}" if invitation_secret is not None else None
    )
    return templates.TemplateResponse(
        request=request,
        name="users.html",
        context={
            "principal": principal,
            "workspace": workspace,
            "role": role,
            "members": members,
            "invitations": invitations,
            "roles": roles,
            "can_manage": can_manage,
            "can_invite": can_invite,
            "invite_denial": invite_denial,
            "invite_url": invite_url,
            "notice": notice,
            "csrf": request.cookies.get(CSRF_COOKIE) or "",
        },
    )


def create_app(
    public_repository=None,
    session_factory=None,
    *,
    public_origin: str | None = None,
) -> FastAPI:
    app = FastAPI(
        title="NEXT Company Workspace",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.public_repository = public_repository or PublicRepository()
    app.state.session_factory = session_factory or SessionLocal
    app.state.cookie_secure = _cookie_secure()
    app.state.demo_mode = os.getenv("NEXTCOMPANY_DEMO_MODE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    app.state.public_origin = _validated_public_origin(
        os.getenv("PUBLIC_ORIGIN", "") if public_origin is None else public_origin
    )

    allowed_hosts = [
        item.strip()
        for item in os.getenv(
            "WORKSPACE_TRUSTED_HOSTS",
            "localhost,127.0.0.1,testserver",
        ).split(",")
        if item.strip()
    ]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    app.add_middleware(RequestBodyLimitMiddleware)
    app.mount(
        "/workspace-static",
        StaticFiles(directory=str(ROOT / "static")),
        name="workspace-static",
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable):
        response = await call_next(request)
        response.headers.update(
            {
                "Content-Security-Policy": (
                    "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                    "script-src 'none'; base-uri 'self'; form-action 'self'; "
                    "frame-ancestors 'none'; object-src 'none'"
                ),
                "Referrer-Policy": "same-origin",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
                "X-Robots-Tag": "noindex, nofollow, nosnippet",
                "Cache-Control": "no-store",
            }
        )
        return response

    @app.get("/app/api/login/csrf")
    def api_login_csrf():
        token = new_login_csrf()
        response = JSONResponse({"csrf_token": token})
        response.set_cookie(
            LOGIN_CSRF_COOKIE,
            token,
            httponly=True,
            secure=app.state.cookie_secure,
            samesite="strict",
            path="/app/api/login",
            max_age=10 * 60,
        )
        return response

    @app.post("/app/api/login")
    async def api_login(request: Request):
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return JSONResponse(
                {"error": {"code": "json_required", "message": "JSON request required"}},
                status_code=415,
            )
        if not login_csrf_valid(
            request.cookies.get(LOGIN_CSRF_COOKIE),
            request.headers.get("x-csrf-token"),
        ):
            return JSONResponse(
                {"error": {"code": "csrf_invalid", "message": "CSRF validation failed"}},
                status_code=403,
            )
        try:
            payload = await request.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            return JSONResponse(
                {"error": {"code": "invalid_request", "message": "Invalid request"}},
                status_code=400,
            )
        with _session_factory(request)() as session:
            attempt = authenticate_customer_attempt(
                session,
                str(payload.get("email") or ""),
                str(payload.get("password") or ""),
            )
            user = attempt.authenticated_user
            if user is None:
                record_login_audit(
                    session,
                    attempt=attempt,
                    workspace_id=None,
                    outcome="denied",
                )
                session.commit()
                return JSONResponse(
                    {"error": {"code": "invalid_credentials", "message": "Invalid email or password"}},
                    status_code=401,
                )
            workspace_ids = active_membership_workspaces(session, user.id)
            if not workspace_ids:
                record_login_audit(
                    session,
                    attempt=attempt,
                    workspace_id=None,
                    outcome="denied",
                )
                session.commit()
                return JSONResponse(
                    {"error": {"code": "membership_required", "message": "Active membership required"}},
                    status_code=403,
                )
            active_workspace_id = workspace_ids[0] if len(workspace_ids) == 1 else None
            token, session_csrf, record = create_customer_session(
                session,
                user=user,
                active_workspace_id=active_workspace_id,
            )
            record_login_audit(
                session,
                attempt=attempt,
                workspace_id=active_workspace_id,
                outcome="success",
            )
            expires_at = record.expires_at
            session.commit()
        response = JSONResponse(
            {
                "authenticated": True,
                "workspace_selection_required": active_workspace_id is None,
                "expires_at": expires_at.isoformat(),
            }
        )
        _set_session_cookies(
            response,
            token=token,
            csrf=session_csrf,
            secure=app.state.cookie_secure,
        )
        response.delete_cookie(LOGIN_CSRF_COOKIE, path="/app/api/login")
        return response

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, return_to: str = "/app"):
        csrf = new_login_csrf()
        response = templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"csrf": csrf, "return_to": safe_return_to(return_to)},
        )
        response.set_cookie(
            LOGIN_CSRF_COOKIE,
            csrf,
            httponly=True,
            secure=app.state.cookie_secure,
            samesite="strict",
            path="/login",
            max_age=10 * 60,
        )
        return response

    @app.post("/login")
    async def login(request: Request):
        form = await request.form()
        csrf = str(form.get("csrf") or "")
        if not login_csrf_valid(request.cookies.get(LOGIN_CSRF_COOKIE), csrf):
            refreshed_csrf = new_login_csrf()
            response = templates.TemplateResponse(
                request=request,
                name="login.html",
                context={
                    "csrf": refreshed_csrf,
                    "return_to": safe_return_to(str(form.get("return_to") or "")),
                    "error": "Сессия формы истекла. Повторите вход.",
                },
                status_code=403,
            )
            response.set_cookie(
                LOGIN_CSRF_COOKIE,
                refreshed_csrf,
                httponly=True,
                secure=app.state.cookie_secure,
                samesite="strict",
                path="/login",
                max_age=10 * 60,
            )
            return response
        with _session_factory(request)() as session:
            attempt = authenticate_customer_attempt(
                session,
                str(form.get("email") or ""),
                str(form.get("password") or ""),
            )
            user = attempt.authenticated_user
            if user is None:
                record_login_audit(
                    session,
                    attempt=attempt,
                    workspace_id=None,
                    outcome="denied",
                )
                session.commit()
                return templates.TemplateResponse(
                    request=request,
                    name="login.html",
                    context={
                        "csrf": csrf,
                        "return_to": safe_return_to(str(form.get("return_to") or "")),
                        "error": "Неверный email или пароль.",
                    },
                    status_code=401,
                )
            workspace_ids = active_membership_workspaces(session, user.id)
            if not workspace_ids:
                record_login_audit(
                    session,
                    attempt=attempt,
                    workspace_id=None,
                    outcome="denied",
                )
                session.commit()
                return templates.TemplateResponse(
                    request=request,
                    name="error.html",
                    context={
                        "code": "membership_required",
                        "message": "У пользователя нет активного рабочего пространства.",
                    },
                    status_code=403,
                )
            active_workspace_id = workspace_ids[0] if len(workspace_ids) == 1 else None
            token, session_csrf, _record = create_customer_session(
                session,
                user=user,
                active_workspace_id=active_workspace_id,
            )
            record_login_audit(
                session,
                attempt=attempt,
                workspace_id=active_workspace_id,
                outcome="success",
            )
            session.commit()
        intended = safe_return_to(str(form.get("return_to") or ""))
        target = intended if active_workspace_id is not None else "/workspace/select"
        if active_workspace_id is None and intended != "/app":
            target = f"/workspace/select?{urlencode({'return_to': intended})}"
        response = RedirectResponse(target, status_code=303)
        _set_session_cookies(
            response,
            token=token,
            csrf=session_csrf,
            secure=app.state.cookie_secure,
        )
        response.delete_cookie(LOGIN_CSRF_COOKIE, path="/login")
        return response

    @app.post("/logout")
    async def logout(request: Request):
        form = await request.form()
        with _session_factory(request)() as session:
            try:
                principal = _require_principal(request, session)
                _verify_post_csrf(request, session, principal, str(form.get("csrf") or ""))
                revoke_session(session, principal)
                record_logout_audit(
                    session,
                    user_id=principal.user_id,
                    workspace_id=principal.active_workspace_id,
                )
                session.commit()
            except ActionDenied:
                session.rollback()
        response = RedirectResponse("/login", status_code=303)
        _clear_session_cookies(response)
        return response

    @app.get("/invite/{token}", response_class=HTMLResponse)
    def invite_page(request: Request, token: str):
        with _session_factory(request)() as session:
            try:
                preview = invitation_preview(session, token=token)
            except ActionDenied as exc:
                return _html_error(request, exc)
        csrf = new_login_csrf()
        response = templates.TemplateResponse(
            request=request,
            name="invite.html",
            context={"preview": preview, "token": token, "csrf": csrf},
        )
        response.set_cookie(
            INVITE_CSRF_COOKIE,
            csrf,
            httponly=True,
            secure=app.state.cookie_secure,
            samesite="strict",
            path="/invite",
            max_age=10 * 60,
        )
        return response

    @app.post("/invite/{token}")
    async def invite_accept(request: Request, token: str):
        form = await request.form()
        csrf = str(form.get("csrf") or "")
        if not login_csrf_valid(request.cookies.get(INVITE_CSRF_COOKIE), csrf):
            return _html_error(
                request,
                ActionDenied(
                    "csrf_invalid",
                    "Сессия формы истекла. Откройте приглашение снова.",
                    status_code=403,
                ),
            )
        with _session_factory(request)() as session:
            try:
                accepted = accept_invitation(
                    session,
                    token=token,
                    password=str(form.get("password") or ""),
                    password_confirmation=str(form.get("password_confirmation") or ""),
                )
                session_token, session_csrf, _record = create_customer_session(
                    session,
                    user=accepted.user,
                    active_workspace_id=accepted.workspace_id,
                )
                session.commit()
            except ActionDenied as exc:
                session.rollback()
                try:
                    preview = invitation_preview(session, token=token)
                except ActionDenied:
                    return _html_error(request, exc)
                refreshed_csrf = new_login_csrf()
                response = templates.TemplateResponse(
                    request=request,
                    name="invite.html",
                    context={
                        "preview": preview,
                        "token": token,
                        "csrf": refreshed_csrf,
                        "error": exc.message,
                    },
                    status_code=exc.status_code,
                )
                response.set_cookie(
                    INVITE_CSRF_COOKIE,
                    refreshed_csrf,
                    httponly=True,
                    secure=app.state.cookie_secure,
                    samesite="strict",
                    path="/invite",
                    max_age=10 * 60,
                )
                return response
        response = RedirectResponse("/app", status_code=303)
        _set_session_cookies(
            response,
            token=session_token,
            csrf=session_csrf,
            secure=app.state.cookie_secure,
        )
        response.delete_cookie(INVITE_CSRF_COOKIE, path="/invite")
        return response

    @app.get("/workspace/select", response_class=HTMLResponse)
    def workspace_select(request: Request, return_to: str = "/app"):
        intended = safe_return_to(return_to)
        with _session_factory(request)() as session:
            try:
                principal = _require_principal(request, session)
                workspaces = list_active_workspaces(session, principal.user_id)
            except ActionDenied as exc:
                return _html_error(request, exc)
        return templates.TemplateResponse(
            request=request,
            name="select_workspace.html",
            context={
                "principal": principal,
                "workspaces": workspaces,
                "csrf": request.cookies.get(CSRF_COOKIE) or "",
                "return_to": intended,
            },
        )

    @app.post("/workspace/select")
    async def workspace_select_post(request: Request):
        form = await request.form()
        with _session_factory(request)() as session:
            principal = None
            try:
                principal = _require_principal(request, session)
                _verify_post_csrf(request, session, principal, str(form.get("csrf") or ""))
                workspace_id = UUID(str(form.get("workspace_id") or ""))
                set_active_workspace(session, principal, workspace_id)
                record_workspace_selection_audit(
                    session,
                    user_id=principal.user_id,
                    workspace_id=workspace_id,
                    outcome="success",
                )
                session.commit()
            except (ValueError, ActionDenied, PermissionError) as exc:
                session.rollback()
                if principal is not None:
                    record_workspace_selection_audit(
                        session,
                        user_id=principal.user_id,
                        workspace_id=None,
                        outcome="denied",
                    )
                    session.commit()
                denied = exc if isinstance(exc, ActionDenied) else ActionDenied(
                    "workspace_selection_denied",
                    "Не удалось выбрать рабочее пространство.",
                )
                return _html_error(request, denied)
        return RedirectResponse(
            safe_return_to(str(form.get("return_to") or "")),
            status_code=303,
        )

    @app.get("/app/users", response_class=HTMLResponse)
    def users_page(request: Request, notice: str = ""):
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(
                    request, session, permission="workspace.view"
                )
                return _users_template_response(
                    request,
                    session,
                    principal,
                    context,
                    notice=notice,
                )
            except ActionDenied as exc:
                if exc.code == "authentication_required":
                    return RedirectResponse("/login?return_to=/app/users", status_code=303)
                if exc.code == "workspace_required":
                    return RedirectResponse(
                        "/workspace/select?return_to=/app/users", status_code=303
                    )
                return _html_error(request, exc)

    @app.post("/app/users/invitations")
    async def users_invite(request: Request):
        form = await request.form()
        with _session_factory(request)() as session:
            try:
                principal = _require_principal(request, session)
                _verify_post_csrf(request, session, principal, str(form.get("csrf") or ""))
                if principal.active_workspace_id is None:
                    raise ActionDenied("workspace_required", "Выберите рабочее пространство.")
                secret = create_invitation(
                    session,
                    user_id=principal.user_id,
                    workspace_id=principal.active_workspace_id,
                    email=str(form.get("email") or ""),
                    role_id=_request_uuid(form.get("role_id"), field="role_id"),
                )
                session.commit()
                context = authorize(
                    session,
                    user_id=principal.user_id,
                    workspace_id=principal.active_workspace_id,
                    permission_key="workspace.view",
                )
                return _users_template_response(
                    request,
                    session,
                    principal,
                    context,
                    invitation_secret=secret,
                    notice="invite_created",
                )
            except ActionDenied as exc:
                session.rollback()
                return _html_error(request, exc)

    @app.post("/app/users/invitations/{invitation_id}/reissue")
    async def users_invite_reissue(request: Request, invitation_id: UUID):
        form = await request.form()
        with _session_factory(request)() as session:
            try:
                principal = _require_principal(request, session)
                _verify_post_csrf(request, session, principal, str(form.get("csrf") or ""))
                if principal.active_workspace_id is None:
                    raise ActionDenied("workspace_required", "Выберите рабочее пространство.")
                secret = reissue_invitation(
                    session,
                    user_id=principal.user_id,
                    workspace_id=principal.active_workspace_id,
                    invitation_id=invitation_id,
                )
                session.commit()
                context = authorize(
                    session,
                    user_id=principal.user_id,
                    workspace_id=principal.active_workspace_id,
                    permission_key="workspace.view",
                )
                return _users_template_response(
                    request,
                    session,
                    principal,
                    context,
                    invitation_secret=secret,
                    notice="invite_reissued",
                )
            except ActionDenied as exc:
                session.rollback()
                return _html_error(request, exc)

    @app.post("/app/users/invitations/{invitation_id}/revoke")
    async def users_invite_revoke(request: Request, invitation_id: UUID):
        form = await request.form()
        with _session_factory(request)() as session:
            try:
                principal = _require_principal(request, session)
                _verify_post_csrf(request, session, principal, str(form.get("csrf") or ""))
                if principal.active_workspace_id is None:
                    raise ActionDenied("workspace_required", "Выберите рабочее пространство.")
                revoke_invitation(
                    session,
                    user_id=principal.user_id,
                    workspace_id=principal.active_workspace_id,
                    invitation_id=invitation_id,
                )
                session.commit()
            except ActionDenied as exc:
                session.rollback()
                return _html_error(request, exc)
        return RedirectResponse("/app/users?notice=invite_revoked", status_code=303)

    @app.post("/app/users/members/{membership_id}/role")
    async def users_member_role(request: Request, membership_id: UUID):
        form = await request.form()
        with _session_factory(request)() as session:
            try:
                principal = _require_principal(request, session)
                _verify_post_csrf(request, session, principal, str(form.get("csrf") or ""))
                if principal.active_workspace_id is None:
                    raise ActionDenied("workspace_required", "Выберите рабочее пространство.")
                change_member_role(
                    session,
                    user_id=principal.user_id,
                    workspace_id=principal.active_workspace_id,
                    membership_id=membership_id,
                    role_id=_request_uuid(form.get("role_id"), field="role_id"),
                )
                session.commit()
            except ActionDenied as exc:
                session.rollback()
                return _html_error(request, exc)
        return RedirectResponse("/app/users?notice=role_changed", status_code=303)

    @app.post("/app/users/members/{membership_id}/status")
    async def users_member_status(request: Request, membership_id: UUID):
        form = await request.form()
        with _session_factory(request)() as session:
            try:
                principal = _require_principal(request, session)
                _verify_post_csrf(request, session, principal, str(form.get("csrf") or ""))
                if principal.active_workspace_id is None:
                    raise ActionDenied("workspace_required", "Выберите рабочее пространство.")
                member = change_member_status(
                    session,
                    user_id=principal.user_id,
                    workspace_id=principal.active_workspace_id,
                    membership_id=membership_id,
                    status=str(form.get("status") or ""),
                )
                session.commit()
            except ActionDenied as exc:
                session.rollback()
                return _html_error(request, exc)
        return RedirectResponse(
            f"/app/users?notice=member_{member.status}", status_code=303
        )

    @app.get("/app/settings", response_class=HTMLResponse)
    def settings_page(request: Request, notice: str = ""):
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(
                    request, session, permission="workspace.view"
                )
                workspace, role = _workspace_shell(session, context)
                settings = get_workspace_settings(
                    session,
                    user_id=principal.user_id,
                    workspace_id=context.workspace_id,
                )
                can_manage, _denial, _limit = action_state(
                    session,
                    user_id=principal.user_id,
                    workspace_id=context.workspace_id,
                    permission_key="workspace.settings.manage",
                )
            except ActionDenied as exc:
                if exc.code == "authentication_required":
                    return RedirectResponse("/login?return_to=/app/settings", status_code=303)
                if exc.code == "workspace_required":
                    return RedirectResponse(
                        "/workspace/select?return_to=/app/settings", status_code=303
                    )
                return _html_error(request, exc)
        return templates.TemplateResponse(
            request=request,
            name="settings.html",
            context={
                "principal": principal,
                "workspace": workspace,
                "role": role,
                "settings": settings,
                "can_manage": can_manage,
                "notice": notice,
                "csrf": request.cookies.get(CSRF_COOKIE) or "",
            },
        )

    @app.post("/app/settings/workspace")
    async def settings_workspace_update(request: Request):
        form = await request.form()
        with _session_factory(request)() as session:
            try:
                principal = _require_principal(request, session)
                _verify_post_csrf(request, session, principal, str(form.get("csrf") or ""))
                if principal.active_workspace_id is None:
                    raise ActionDenied("workspace_required", "Выберите рабочее пространство.")
                update_workspace_name(
                    session,
                    user_id=principal.user_id,
                    workspace_id=principal.active_workspace_id,
                    name=str(form.get("name") or ""),
                )
                session.commit()
            except ActionDenied as exc:
                session.rollback()
                return _html_error(request, exc)
        return RedirectResponse("/app/settings?notice=workspace_renamed", status_code=303)

    @app.get("/app/api/users")
    def api_users(request: Request):
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(
                    request, session, permission="workspace.view"
                )
                members = list_members(
                    session,
                    user_id=principal.user_id,
                    workspace_id=context.workspace_id,
                )
                invitations = list_invitations(
                    session,
                    user_id=principal.user_id,
                    workspace_id=context.workspace_id,
                )
                roles = list_system_roles(
                    session,
                    user_id=principal.user_id,
                    workspace_id=context.workspace_id,
                )
            except ActionDenied as exc:
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        return JSONResponse(
            {
                "members": [_member_payload(item) for item in members],
                "invitations": [_invitation_payload(item) for item in invitations],
                "roles": [_role_payload(item) for item in roles],
            }
        )

    @app.post("/app/api/invitations")
    async def api_invitation_create(request: Request):
        try:
            payload = await request.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            return JSONResponse(
                {"error": {"code": "invalid_request", "message": "Invalid request"}},
                status_code=400,
            )
        with _session_factory(request)() as session:
            try:
                principal = _require_principal(request, session)
                _verify_post_csrf(
                    request, session, principal, request.headers.get("x-csrf-token")
                )
                if principal.active_workspace_id is None:
                    raise ActionDenied("workspace_required", "Выберите рабочее пространство.")
                secret = create_invitation(
                    session,
                    user_id=principal.user_id,
                    workspace_id=principal.active_workspace_id,
                    email=str(payload.get("email") or ""),
                    role_id=_request_uuid(payload.get("role_id"), field="role_id"),
                )
                session.commit()
            except ActionDenied as exc:
                session.rollback()
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        return JSONResponse(
            {
                "invitation": _invitation_payload(secret.invitation),
                "invite_url": f"/invite/{secret.token}",
            },
            status_code=201,
        )

    @app.post("/app/api/invitations/{invitation_id}/reissue")
    def api_invitation_reissue(request: Request, invitation_id: UUID):
        with _session_factory(request)() as session:
            try:
                principal = _require_principal(request, session)
                _verify_post_csrf(
                    request, session, principal, request.headers.get("x-csrf-token")
                )
                if principal.active_workspace_id is None:
                    raise ActionDenied("workspace_required", "Выберите рабочее пространство.")
                secret = reissue_invitation(
                    session,
                    user_id=principal.user_id,
                    workspace_id=principal.active_workspace_id,
                    invitation_id=invitation_id,
                )
                session.commit()
            except ActionDenied as exc:
                session.rollback()
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        return JSONResponse(
            {
                "invitation": _invitation_payload(secret.invitation),
                "invite_url": f"/invite/{secret.token}",
            }
        )

    @app.delete("/app/api/invitations/{invitation_id}")
    def api_invitation_revoke(request: Request, invitation_id: UUID):
        with _session_factory(request)() as session:
            try:
                principal = _require_principal(request, session)
                _verify_post_csrf(
                    request, session, principal, request.headers.get("x-csrf-token")
                )
                if principal.active_workspace_id is None:
                    raise ActionDenied("workspace_required", "Выберите рабочее пространство.")
                invitation = revoke_invitation(
                    session,
                    user_id=principal.user_id,
                    workspace_id=principal.active_workspace_id,
                    invitation_id=invitation_id,
                )
                session.commit()
            except ActionDenied as exc:
                session.rollback()
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        return JSONResponse({"invitation": _invitation_payload(invitation)})

    @app.patch("/app/api/members/{membership_id}/role")
    async def api_member_role(request: Request, membership_id: UUID):
        try:
            payload = await request.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            return JSONResponse(
                {"error": {"code": "invalid_request", "message": "Invalid request"}},
                status_code=400,
            )
        with _session_factory(request)() as session:
            try:
                principal = _require_principal(request, session)
                _verify_post_csrf(
                    request, session, principal, request.headers.get("x-csrf-token")
                )
                if principal.active_workspace_id is None:
                    raise ActionDenied("workspace_required", "Выберите рабочее пространство.")
                member = change_member_role(
                    session,
                    user_id=principal.user_id,
                    workspace_id=principal.active_workspace_id,
                    membership_id=membership_id,
                    role_id=_request_uuid(payload.get("role_id"), field="role_id"),
                )
                session.commit()
            except ActionDenied as exc:
                session.rollback()
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        return JSONResponse({"member": _member_payload(member)})

    @app.patch("/app/api/members/{membership_id}/status")
    async def api_member_status(request: Request, membership_id: UUID):
        try:
            payload = await request.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            return JSONResponse(
                {"error": {"code": "invalid_request", "message": "Invalid request"}},
                status_code=400,
            )
        with _session_factory(request)() as session:
            try:
                principal = _require_principal(request, session)
                _verify_post_csrf(
                    request, session, principal, request.headers.get("x-csrf-token")
                )
                if principal.active_workspace_id is None:
                    raise ActionDenied("workspace_required", "Выберите рабочее пространство.")
                member = change_member_status(
                    session,
                    user_id=principal.user_id,
                    workspace_id=principal.active_workspace_id,
                    membership_id=membership_id,
                    status=str(payload.get("status") or ""),
                )
                session.commit()
            except ActionDenied as exc:
                session.rollback()
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        return JSONResponse({"member": _member_payload(member)})

    @app.get("/app/api/settings")
    def api_settings(request: Request):
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(
                    request, session, permission="workspace.view"
                )
                settings = get_workspace_settings(
                    session,
                    user_id=principal.user_id,
                    workspace_id=context.workspace_id,
                )
            except ActionDenied as exc:
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        return JSONResponse(_settings_payload(settings))

    @app.patch("/app/api/settings/workspace")
    async def api_settings_workspace(request: Request):
        try:
            payload = await request.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            return JSONResponse(
                {"error": {"code": "invalid_request", "message": "Invalid request"}},
                status_code=400,
            )
        with _session_factory(request)() as session:
            try:
                principal = _require_principal(request, session)
                _verify_post_csrf(
                    request, session, principal, request.headers.get("x-csrf-token")
                )
                if principal.active_workspace_id is None:
                    raise ActionDenied("workspace_required", "Выберите рабочее пространство.")
                workspace = update_workspace_name(
                    session,
                    user_id=principal.user_id,
                    workspace_id=principal.active_workspace_id,
                    name=str(payload.get("name") or ""),
                )
                response_payload = {
                    "workspace": {
                        "id": str(workspace.id),
                        "name": workspace.name,
                        "status": workspace.status,
                    }
                }
                session.commit()
            except ActionDenied as exc:
                session.rollback()
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        return JSONResponse(response_payload)

    @app.get("/app", response_class=HTMLResponse)
    def home(request: Request):
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(
                    request, session, permission="workspace.view"
                )
                dashboard = get_workspace_dashboard(
                    session,
                    user_id=principal.user_id,
                    workspace_id=context.workspace_id,
                )
                workspace, role = _workspace_shell(session, context)
            except ActionDenied as exc:
                if exc.code == "authentication_required":
                    return RedirectResponse("/login?return_to=/app", status_code=303)
                if exc.code == "workspace_required":
                    return RedirectResponse("/workspace/select", status_code=303)
                return _html_error(request, exc)
        return templates.TemplateResponse(
            request=request,
            name="home.html",
            context={
                "principal": principal,
                "workspace": workspace,
                "role": role,
                "dashboard": dashboard,
                "csrf": request.cookies.get(CSRF_COOKIE) or "",
            },
        )

    @app.get("/app/search", response_class=HTMLResponse)
    def search(request: Request, q: str = ""):
        query = " ".join(str(q or "").split())[:160]
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(
                    request, session, permission="company.search"
                )
                workspace, role = _workspace_shell(session, context)
            except ActionDenied as exc:
                if exc.code == "authentication_required":
                    intended = "/app/search"
                    if query:
                        intended = f"{intended}?{urlencode({'q': query})}"
                    return RedirectResponse(
                        f"/login?{urlencode({'return_to': intended})}",
                        status_code=303,
                    )
                if exc.code == "workspace_required":
                    return RedirectResponse(
                        f"/workspace/select?{urlencode({'return_to': '/app/search'})}",
                        status_code=303,
                    )
                return _html_error(request, exc)
        results = _public_repository(request).search(query) if query else []
        return templates.TemplateResponse(
            request=request,
            name="search.html",
            context={
                "principal": principal,
                "workspace": workspace,
                "role": role,
                "query": query,
                "results": results,
                "csrf": request.cookies.get(CSRF_COOKIE) or "",
            },
        )

    @app.get("/app/companies/{inn}", response_class=HTMLResponse)
    def company_card(request: Request, inn: str, notice: str = ""):
        if not valid_legal_inn(inn):
            raise StarletteHTTPException(status_code=404)
        projection = _public_repository(request).get_company(inn)
        if projection is None:
            raise StarletteHTTPException(status_code=404)
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(
                    request, session, permission="company.view"
                )
                workspace, role = _workspace_shell(session, context)
                actions = _card_context(session, principal, projection)
            except ActionDenied as exc:
                if exc.code == "authentication_required":
                    return RedirectResponse(
                        f"/login?{urlencode({'return_to': f'/app/companies/{inn}'})}",
                        status_code=303,
                    )
                if exc.code == "workspace_required":
                    return RedirectResponse(
                        f"/workspace/select?{urlencode({'return_to': f'/app/companies/{inn}'})}",
                        status_code=303,
                    )
                return _html_error(request, exc)
        return templates.TemplateResponse(
            request=request,
            name="company.html",
            context={
                "principal": principal,
                "workspace": workspace,
                "role": role,
                "projection": projection,
                "actions": actions,
                "notice": notice if notice in {"saved", "unsaved"} else "",
                "csrf": request.cookies.get(CSRF_COOKIE) or "",
            },
        )

    @app.post("/app/companies/{inn}/save")
    async def save(request: Request, inn: str):
        form = await request.form()
        with _session_factory(request)() as session:
            try:
                principal = _require_principal(request, session)
                _verify_post_csrf(request, session, principal, str(form.get("csrf") or ""))
                if principal.active_workspace_id is None:
                    raise ActionDenied("workspace_required", "Выберите рабочее пространство.")
                save_company(
                    session,
                    user_id=principal.user_id,
                    workspace_id=principal.active_workspace_id,
                    inn=inn,
                )
                session.commit()
            except ActionDenied as exc:
                if exc.code == "quota_exceeded":
                    session.commit()
                else:
                    session.rollback()
                return _html_error(request, exc)
        return RedirectResponse(
            f"/app/companies/{inn}?notice=saved",
            status_code=303,
        )

    @app.post("/app/companies/{inn}/unsave")
    async def unsave(request: Request, inn: str):
        form = await request.form()
        with _session_factory(request)() as session:
            try:
                principal = _require_principal(request, session)
                _verify_post_csrf(request, session, principal, str(form.get("csrf") or ""))
                if principal.active_workspace_id is None:
                    raise ActionDenied("workspace_required", "Выберите рабочее пространство.")
                unsave_company(
                    session,
                    user_id=principal.user_id,
                    workspace_id=principal.active_workspace_id,
                    inn=inn,
                )
                session.commit()
            except ActionDenied as exc:
                session.rollback()
                return _html_error(request, exc)
        return RedirectResponse(
            safe_return_to(
                str(form.get("return_to") or ""),
                default=f"/app/companies/{inn}?notice=unsaved",
            ),
            status_code=303,
        )

    @app.get("/app/saved", response_class=HTMLResponse)
    def saved(request: Request, q: str = "", notice: str = ""):
        query = " ".join(str(q or "").split())[:160]
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(
                    request, session, permission="company.view"
                )
                entries = saved_companies(
                    session,
                    user_id=principal.user_id,
                    workspace_id=context.workspace_id,
                    query=query,
                )
                workspace, role = _workspace_shell(session, context)
                can_unsave, _unsave_denial, _unsave_limit = action_state(
                    session,
                    user_id=principal.user_id,
                    workspace_id=context.workspace_id,
                    permission_key="company.unsave",
                )
                can_edit_note, _note_denial, _note_limit = action_state(
                    session,
                    user_id=principal.user_id,
                    workspace_id=context.workspace_id,
                    permission_key="company.save",
                )
            except ActionDenied as exc:
                if exc.code == "authentication_required":
                    return RedirectResponse("/login?return_to=/app/saved", status_code=303)
                if exc.code == "workspace_required":
                    return RedirectResponse(
                        "/workspace/select?return_to=/app/saved",
                        status_code=303,
                    )
                return _html_error(request, exc)
        return templates.TemplateResponse(
            request=request,
            name="saved.html",
            context={
                "principal": principal,
                "workspace": workspace,
                "role": role,
                "entries": entries,
                "query": query,
                "notice": notice if notice in {"note_updated", "note_cleared"} else "",
                "can_unsave": can_unsave,
                "can_edit_note": can_edit_note,
                "csrf": request.cookies.get(CSRF_COOKIE) or "",
            },
        )

    @app.post("/app/saved/{saved_company_id}/note")
    async def saved_note_form(request: Request, saved_company_id: UUID):
        form = await request.form()
        with _session_factory(request)() as session:
            try:
                principal = _require_principal(request, session)
                _verify_post_csrf(
                    request,
                    session,
                    principal,
                    str(form.get("csrf") or ""),
                )
                if principal.active_workspace_id is None:
                    raise ActionDenied("workspace_required", "Выберите рабочее пространство.")
                entry, _changed = update_saved_company_note(
                    session,
                    user_id=principal.user_id,
                    workspace_id=principal.active_workspace_id,
                    saved_company_id=saved_company_id,
                    note=str(form.get("note") or ""),
                )
                session.commit()
            except ActionDenied as exc:
                session.rollback()
                return _html_error(request, exc)
        notice = "note_updated" if entry.note else "note_cleared"
        return RedirectResponse(f"/app/saved?notice={notice}", status_code=303)

    @app.post("/app/companies/{inn}/reports")
    async def generate_report_form(request: Request, inn: str):
        form = await request.form()
        with _session_factory(request)() as session:
            try:
                principal = _require_principal(request, session)
                _verify_post_csrf(
                    request,
                    session,
                    principal,
                    str(form.get("csrf") or ""),
                )
                if principal.active_workspace_id is None:
                    raise ActionDenied(
                        "workspace_required", "Выберите рабочее пространство."
                    )
                report = generate_report(
                    session,
                    user_id=principal.user_id,
                    workspace_id=principal.active_workspace_id,
                    inn=inn,
                    projection_repository=_public_repository(request),
                )
                report_id = report.id
                session.commit()
            except ActionDenied as exc:
                if exc.code == "quota_exceeded":
                    session.commit()
                else:
                    session.rollback()
                return _html_error(request, exc)
        return RedirectResponse(f"/app/reports/{report_id}", status_code=303)

    @app.get("/app/reports", response_class=HTMLResponse)
    def reports_page(request: Request, limit: int = 50):
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(
                    request, session, permission="report.view"
                )
                entries = list_reports(
                    session,
                    user_id=principal.user_id,
                    workspace_id=context.workspace_id,
                    limit=limit,
                )
                workspace, role = _workspace_shell(session, context)
            except ActionDenied as exc:
                if exc.code == "authentication_required":
                    return RedirectResponse(
                        "/login?return_to=/app/reports", status_code=303
                    )
                if exc.code == "workspace_required":
                    return RedirectResponse(
                        "/workspace/select?return_to=/app/reports", status_code=303
                    )
                return _html_error(request, exc)
        return templates.TemplateResponse(
            request=request,
            name="reports.html",
            context={
                "principal": principal,
                "workspace": workspace,
                "role": role,
                "reports": entries,
                "csrf": request.cookies.get(CSRF_COOKIE) or "",
            },
        )

    @app.get("/app/reports/{report_id}", response_class=HTMLResponse)
    def report_detail_page(request: Request, report_id: UUID):
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(
                    request, session, permission="report.view"
                )
                report = get_report(
                    session,
                    user_id=principal.user_id,
                    workspace_id=context.workspace_id,
                    report_id=report_id,
                )
                workspace, role = _workspace_shell(session, context)
                snapshot = report.snapshot
            except ActionDenied as exc:
                if exc.code == "authentication_required":
                    return RedirectResponse(
                        f"/login?return_to=/app/reports/{report_id}",
                        status_code=303,
                    )
                if exc.code == "workspace_required":
                    return RedirectResponse(
                        f"/workspace/select?return_to=/app/reports/{report_id}",
                        status_code=303,
                    )
                return _html_error(request, exc)
        return templates.TemplateResponse(
            request=request,
            name="report_detail.html",
            context={
                "principal": principal,
                "workspace": workspace,
                "role": role,
                "report": report,
                "snapshot": snapshot,
                "csrf": request.cookies.get(CSRF_COOKIE) or "",
            },
        )

    @app.get("/app/reports/{report_id}/export.json")
    def report_json_download(request: Request, report_id: UUID):
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(
                    request, session, permission="report.export"
                )
                report = get_report(
                    session,
                    user_id=principal.user_id,
                    workspace_id=context.workspace_id,
                    report_id=report_id,
                    permission_key="report.export",
                )
                content = canonical_json_bytes(report.snapshot)
                filename = report_filename(report, "json")
            except ActionDenied as exc:
                return _html_error(request, exc)
        return Response(
            content=content,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/app/reports/{report_id}/export.csv")
    def report_csv_download(request: Request, report_id: UUID):
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(
                    request, session, permission="report.export"
                )
                report = get_report(
                    session,
                    user_id=principal.user_id,
                    workspace_id=context.workspace_id,
                    report_id=report_id,
                    permission_key="report.export",
                )
                content = report_csv_bytes(report.snapshot)
                filename = report_filename(report, "csv")
            except ActionDenied as exc:
                return _html_error(request, exc)
        return Response(
            content=content,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/app/api/companies/{inn}/reports")
    def api_generate_report(request: Request, inn: str):
        with _session_factory(request)() as session:
            try:
                principal = _require_principal(request, session)
                _verify_post_csrf(
                    request,
                    session,
                    principal,
                    request.headers.get("x-csrf-token"),
                )
                if principal.active_workspace_id is None:
                    raise ActionDenied(
                        "workspace_required", "Выберите рабочее пространство."
                    )
                report = generate_report(
                    session,
                    user_id=principal.user_id,
                    workspace_id=principal.active_workspace_id,
                    inn=inn,
                    projection_repository=_public_repository(request),
                )
                report_id = report.id
                snapshot = report.snapshot
                session.commit()
            except ActionDenied as exc:
                if exc.code == "quota_exceeded":
                    session.commit()
                else:
                    session.rollback()
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        return JSONResponse(
            snapshot,
            status_code=201,
            headers={"Location": f"/app/api/reports/{report_id}"},
        )

    @app.get("/app/api/reports")
    def api_reports(request: Request, limit: int = 50):
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(
                    request, session, permission="report.view"
                )
                entries = list_reports(
                    session,
                    user_id=principal.user_id,
                    workspace_id=context.workspace_id,
                    limit=limit,
                )
            except ActionDenied as exc:
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        return JSONResponse({"items": [report_list_payload(item) for item in entries]})

    @app.get("/app/api/reports/{report_id}")
    def api_report_detail(request: Request, report_id: UUID):
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(
                    request, session, permission="report.view"
                )
                report = get_report(
                    session,
                    user_id=principal.user_id,
                    workspace_id=context.workspace_id,
                    report_id=report_id,
                )
                content = canonical_json_bytes(report.snapshot)
            except ActionDenied as exc:
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        return Response(content=content, media_type="application/json")

    @app.get("/app/api/reports/{report_id}/export.json")
    def api_report_json_download(request: Request, report_id: UUID):
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(
                    request, session, permission="report.export"
                )
                report = get_report(
                    session,
                    user_id=principal.user_id,
                    workspace_id=context.workspace_id,
                    report_id=report_id,
                    permission_key="report.export",
                )
                content = canonical_json_bytes(report.snapshot)
                filename = report_filename(report, "json")
            except ActionDenied as exc:
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        return Response(
            content=content,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/app/api/reports/{report_id}/export.csv")
    def api_report_csv_download(request: Request, report_id: UUID):
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(
                    request, session, permission="report.export"
                )
                report = get_report(
                    session,
                    user_id=principal.user_id,
                    workspace_id=context.workspace_id,
                    report_id=report_id,
                    permission_key="report.export",
                )
                content = report_csv_bytes(report.snapshot)
                filename = report_filename(report, "csv")
            except ActionDenied as exc:
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        return Response(
            content=content,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/app/bulk", response_class=HTMLResponse)
    def bulk_page(request: Request, limit: int = 50):
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(request, session, permission="bulk.view")
                jobs = list_bulk_jobs(session, user_id=principal.user_id, workspace_id=context.workspace_id, limit=limit)
                workspace, role = _workspace_shell(session, context)
                can_create, create_denial, create_limit = action_state(
                    session, user_id=principal.user_id, workspace_id=context.workspace_id, permission_key="bulk.create"
                )
            except ActionDenied as exc:
                if exc.code == "authentication_required":
                    return RedirectResponse("/login?return_to=/app/bulk", status_code=303)
                if exc.code == "workspace_required":
                    return RedirectResponse("/workspace/select?return_to=/app/bulk", status_code=303)
                return _html_error(request, exc)
        return templates.TemplateResponse(
            request=request,
            name="bulk.html",
            context={
                "principal": principal, "workspace": workspace, "role": role,
                "jobs": jobs, "can_create": can_create,
                "create_denial": create_denial, "create_limit": create_limit,
                "csrf": request.cookies.get(CSRF_COOKIE) or "",
            },
        )

    @app.post("/app/bulk")
    async def bulk_create_form(request: Request):
        try:
            with _session_factory(request)() as session:
                _require_active_workspace(request, session, permission="bulk.create")
        except ActionDenied as exc:
            return _html_error(request, exc)
        form = None
        try:
            form = await request.form(max_files=1, max_fields=1, max_part_size=4096)
            upload = form.get("file")
            with _session_factory(request)() as session:
                principal, context = _require_active_workspace(request, session, permission="bulk.create")
                _verify_post_csrf(request, session, principal, str(form.get("csrf") or ""))
                if upload is None or not hasattr(upload, "read"):
                    raise ActionDenied("bulk_file_required", "Выберите CSV-файл.", status_code=422)
                upload_size = getattr(upload, "size", None)
                if upload_size is not None and upload_size > MAX_FILE_BYTES:
                    raise ActionDenied("bulk_file_too_large", "CSV-файл превышает лимит 2 МиБ.", status_code=413)
                content = await upload.read(MAX_FILE_BYTES + 1)
                job = create_bulk_job(
                    session, user_id=principal.user_id, workspace_id=context.workspace_id,
                    filename=getattr(upload, "filename", None), content=content,
                )
                job_id = job.id
                session.commit()
        except ActionDenied as exc:
            return _html_error(request, exc)
        finally:
            if form is not None:
                await form.close()
        return RedirectResponse(f"/app/bulk/{job_id}", status_code=303)

    @app.get("/app/bulk/{job_id}", response_class=HTMLResponse)
    def bulk_detail_page(
        request: Request, job_id: UUID, status: str = "", q: str = "",
        page: int = 1, page_size: int = 50,
    ):
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(request, session, permission="bulk.view")
                job = get_bulk_job(session, user_id=principal.user_id, workspace_id=context.workspace_id, job_id=job_id)
                items_page = list_bulk_items(
                    session, user_id=principal.user_id, workspace_id=context.workspace_id,
                    job_id=job_id, status=status or None, query=q, page=page, page_size=page_size,
                )
                workspace, role = _workspace_shell(session, context)
                can_manage = action_state(session, user_id=principal.user_id, workspace_id=context.workspace_id, permission_key="bulk.manage")[0]
            except ActionDenied as exc:
                if exc.code == "authentication_required":
                    return RedirectResponse(f"/login?return_to=/app/bulk/{job_id}", status_code=303)
                if exc.code == "workspace_required":
                    return RedirectResponse(f"/workspace/select?return_to=/app/bulk/{job_id}", status_code=303)
                return _html_error(request, exc)
        return templates.TemplateResponse(
            request=request,
            name="bulk_detail.html",
            context={
                "principal": principal, "workspace": workspace, "role": role,
                "job": job, "job_data": job_payload(job), "items_page": items_page,
                "status_filter": status, "query": q, "item_statuses": sorted(ITEM_STATUSES),
                "can_manage": can_manage, "csrf": request.cookies.get(CSRF_COOKIE) or "",
            },
        )

    async def _bulk_html_mutation(request: Request, job_id: UUID, operation):
        form = await request.form()
        with _session_factory(request)() as session:
            try:
                principal = _require_principal(request, session)
                _verify_post_csrf(request, session, principal, str(form.get("csrf") or ""))
                if principal.active_workspace_id is None:
                    raise ActionDenied("workspace_required", "Выберите рабочее пространство.")
                kwargs = dict(session=session, user_id=principal.user_id, workspace_id=principal.active_workspace_id, job_id=job_id)
                if operation is process_bulk_job_chunk:
                    kwargs["projection_repository"] = _public_repository(request)
                operation(**kwargs)
                session.commit()
            except ActionDenied as exc:
                session.rollback()
                return _html_error(request, exc)
        return RedirectResponse(f"/app/bulk/{job_id}", status_code=303)

    @app.post("/app/bulk/{job_id}/process-next")
    async def bulk_process_form(request: Request, job_id: UUID):
        return await _bulk_html_mutation(request, job_id, process_bulk_job_chunk)

    @app.post("/app/bulk/{job_id}/cancel")
    async def bulk_cancel_form(request: Request, job_id: UUID):
        return await _bulk_html_mutation(request, job_id, cancel_bulk_job)

    @app.post("/app/bulk/{job_id}/resume")
    async def bulk_resume_form(request: Request, job_id: UUID):
        return await _bulk_html_mutation(request, job_id, resume_bulk_job)

    @app.post("/app/bulk/{job_id}/retry")
    async def bulk_retry_form(request: Request, job_id: UUID):
        return await _bulk_html_mutation(request, job_id, retry_bulk_job)

    @app.get("/app/bulk/{job_id}/export.json")
    def bulk_json_download(request: Request, job_id: UUID):
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(request, session, permission="bulk.export")
                job = get_bulk_job(session, user_id=principal.user_id, workspace_id=context.workspace_id, job_id=job_id, permission_key="bulk.export")
                content = bulk_json_bytes(session, user_id=principal.user_id, workspace_id=context.workspace_id, job_id=job_id)
                filename = bulk_filename(job, "json")
                record_bulk_export(session, user_id=principal.user_id, workspace_id=context.workspace_id, job_id=job_id, format="json")
                session.commit()
            except ActionDenied as exc:
                return _html_error(request, exc)
        return Response(content=content, media_type="application/json", headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.get("/app/bulk/{job_id}/export.csv")
    def bulk_csv_download(request: Request, job_id: UUID):
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(request, session, permission="bulk.export")
                job = get_bulk_job(session, user_id=principal.user_id, workspace_id=context.workspace_id, job_id=job_id, permission_key="bulk.export")
                content = bulk_csv_bytes(session, user_id=principal.user_id, workspace_id=context.workspace_id, job_id=job_id)
                filename = bulk_filename(job, "csv")
                record_bulk_export(session, user_id=principal.user_id, workspace_id=context.workspace_id, job_id=job_id, format="csv")
                session.commit()
            except ActionDenied as exc:
                return _html_error(request, exc)
        return Response(content=content, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.post("/app/api/bulk-jobs")
    async def api_bulk_create(request: Request):
        try:
            with _session_factory(request)() as session:
                principal, _context = _require_active_workspace(request, session, permission="bulk.create")
                _verify_post_csrf(request, session, principal, request.headers.get("x-csrf-token"))
        except ActionDenied as exc:
            return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        form = None
        try:
            form = await request.form(max_files=1, max_fields=0, max_part_size=4096)
            upload = form.get("file")
            with _session_factory(request)() as session:
                principal, context = _require_active_workspace(request, session, permission="bulk.create")
                _verify_post_csrf(request, session, principal, request.headers.get("x-csrf-token"))
                if upload is None or not hasattr(upload, "read"):
                    raise ActionDenied("bulk_file_required", "Выберите CSV-файл.", status_code=422)
                upload_size = getattr(upload, "size", None)
                if upload_size is not None and upload_size > MAX_FILE_BYTES:
                    raise ActionDenied("bulk_file_too_large", "CSV-файл превышает лимит 2 МиБ.", status_code=413)
                content = await upload.read(MAX_FILE_BYTES + 1)
                job = create_bulk_job(session, user_id=principal.user_id, workspace_id=context.workspace_id, filename=getattr(upload, "filename", None), content=content)
                payload = job_payload(job)
                session.commit()
        except ActionDenied as exc:
            return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        finally:
            if form is not None:
                await form.close()
        return JSONResponse(payload, status_code=201, headers={"Location": f"/app/api/bulk-jobs/{payload['job_id']}"})

    @app.get("/app/api/bulk-jobs")
    def api_bulk_jobs(request: Request, limit: int = 50):
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(request, session, permission="bulk.view")
                jobs = list_bulk_jobs(session, user_id=principal.user_id, workspace_id=context.workspace_id, limit=limit)
            except ActionDenied as exc:
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        return JSONResponse({"items": [job_payload(job) for job in jobs]})

    @app.get("/app/api/bulk-jobs/{job_id}")
    def api_bulk_job(request: Request, job_id: UUID, status: str = "", q: str = "", page: int = 1, page_size: int = 50):
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(request, session, permission="bulk.view")
                job = get_bulk_job(session, user_id=principal.user_id, workspace_id=context.workspace_id, job_id=job_id)
                items = list_bulk_items(session, user_id=principal.user_id, workspace_id=context.workspace_id, job_id=job_id, status=status or None, query=q, page=page, page_size=page_size)
                payload = job_payload(job)
                payload["items"] = [item_payload(item) for item in items.items]
                payload["pagination"] = {"page": items.page, "page_size": items.page_size, "total": items.total, "pages": items.pages}
            except ActionDenied as exc:
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        return JSONResponse(payload)

    def _api_bulk_mutation(request: Request, job_id: UUID, operation):
        with _session_factory(request)() as session:
            try:
                principal = _require_principal(request, session)
                _verify_post_csrf(request, session, principal, request.headers.get("x-csrf-token"))
                if principal.active_workspace_id is None:
                    raise ActionDenied("workspace_required", "Выберите рабочее пространство.")
                kwargs = dict(session=session, user_id=principal.user_id, workspace_id=principal.active_workspace_id, job_id=job_id)
                if operation is process_bulk_job_chunk:
                    kwargs["projection_repository"] = _public_repository(request)
                job = operation(**kwargs)
                payload = job_payload(job)
                session.commit()
            except ActionDenied as exc:
                session.rollback()
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        return JSONResponse(payload)

    @app.post("/app/api/bulk-jobs/{job_id}/process-next")
    def api_bulk_process(request: Request, job_id: UUID):
        return _api_bulk_mutation(request, job_id, process_bulk_job_chunk)

    @app.post("/app/api/bulk-jobs/{job_id}/cancel")
    def api_bulk_cancel(request: Request, job_id: UUID):
        return _api_bulk_mutation(request, job_id, cancel_bulk_job)

    @app.post("/app/api/bulk-jobs/{job_id}/resume")
    def api_bulk_resume(request: Request, job_id: UUID):
        return _api_bulk_mutation(request, job_id, resume_bulk_job)

    @app.post("/app/api/bulk-jobs/{job_id}/retry")
    def api_bulk_retry(request: Request, job_id: UUID):
        return _api_bulk_mutation(request, job_id, retry_bulk_job)

    @app.get("/app/api/bulk-jobs/{job_id}/export.json")
    def api_bulk_json(request: Request, job_id: UUID):
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(request, session, permission="bulk.export")
                job = get_bulk_job(session, user_id=principal.user_id, workspace_id=context.workspace_id, job_id=job_id, permission_key="bulk.export")
                content = bulk_json_bytes(session, user_id=principal.user_id, workspace_id=context.workspace_id, job_id=job_id)
                filename = bulk_filename(job, "json")
                record_bulk_export(session, user_id=principal.user_id, workspace_id=context.workspace_id, job_id=job_id, format="json")
                session.commit()
            except ActionDenied as exc:
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        return Response(content=content, media_type="application/json", headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.get("/app/api/bulk-jobs/{job_id}/export.csv")
    def api_bulk_csv(request: Request, job_id: UUID):
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(request, session, permission="bulk.export")
                job = get_bulk_job(session, user_id=principal.user_id, workspace_id=context.workspace_id, job_id=job_id, permission_key="bulk.export")
                content = bulk_csv_bytes(session, user_id=principal.user_id, workspace_id=context.workspace_id, job_id=job_id)
                filename = bulk_filename(job, "csv")
                record_bulk_export(session, user_id=principal.user_id, workspace_id=context.workspace_id, job_id=job_id, format="csv")
                session.commit()
            except ActionDenied as exc:
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        return Response(content=content, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.get("/app/companies/{inn}/monitoring", response_class=HTMLResponse)
    def monitoring_entry(request: Request, inn: str):
        if not valid_legal_inn(inn):
            raise StarletteHTTPException(status_code=404)
        projection = _public_repository(request).get_company(inn)
        if projection is None:
            raise StarletteHTTPException(status_code=404)
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(
                    request,
                    session,
                    permission="company.view",
                )
                workspace, role = _workspace_shell(session, context)
                actions = _card_context(session, principal, projection)
                if not actions["is_saved"]:
                    raise ActionDenied(
                        "saved_company_required",
                        "Сначала сохраните компанию в этом Workspace.",
                        status_code=409,
                    )
            except ActionDenied as exc:
                if exc.code == "authentication_required":
                    intended = f"/app/companies/{inn}/monitoring"
                    return RedirectResponse(
                        f"/login?{urlencode({'return_to': intended})}",
                        status_code=303,
                    )
                return _html_error(request, exc)
        return templates.TemplateResponse(
            request=request,
            name="monitoring.html",
            context={
                "principal": principal,
                "workspace": workspace,
                "role": role,
                "company": projection.company,
                "monitoring": actions["monitoring"],
                "csrf": request.cookies.get(CSRF_COOKIE) or "",
            },
        )

    @app.post("/app/companies/{inn}/monitoring/{action}")
    async def monitoring_action_form(request: Request, inn: str, action: str):
        if action not in {"enable", "pause", "resume"}:
            raise StarletteHTTPException(status_code=404)
        form = await request.form()
        with _session_factory(request)() as session:
            try:
                principal = _require_principal(request, session)
                _verify_post_csrf(request, session, principal, str(form.get("csrf") or ""))
                if principal.active_workspace_id is None:
                    raise ActionDenied("workspace_required", "Выберите рабочее пространство.")
                operations = {
                    "enable": subscribe_company,
                    "pause": pause_subscription,
                    "resume": resume_subscription,
                }
                operations[action](
                    session,
                    user_id=principal.user_id,
                    workspace_id=principal.active_workspace_id,
                    inn=inn,
                )
                session.commit()
            except ActionDenied as exc:
                session.rollback()
                return _html_error(request, exc)
        return RedirectResponse(
            safe_return_to(
                str(form.get("return_to") or ""),
                default=f"/app/companies/{inn}/monitoring",
            ),
            status_code=303,
        )

    @app.get("/app/monitoring", response_class=HTMLResponse)
    def monitoring_feed_page(
        request: Request,
        state: str = "all",
        severity: str = "",
    ):
        read_state = str(state or "all").strip().lower()
        severity_filter = str(severity or "").strip().upper()
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(
                    request, session, permission="workspace.view"
                )
                can_manage_monitoring, monitoring_denial, _monitoring_limit = action_state(
                    session,
                    user_id=principal.user_id,
                    workspace_id=context.workspace_id,
                    permission_key="monitoring.manage",
                )
                if monitoring_denial == "permission_denied":
                    raise ActionDenied(
                        "permission_denied",
                        "Недостаточно прав для просмотра мониторинга.",
                    )
                if can_manage_monitoring:
                    entries = list_workspace_feed(
                        session,
                        user_id=principal.user_id,
                        workspace_id=context.workspace_id,
                        read_state=read_state,
                        severity=severity_filter or None,
                    )
                    subscriptions = list_workspace_subscriptions(
                        session,
                        user_id=principal.user_id,
                        workspace_id=context.workspace_id,
                    )
                else:
                    entries = ()
                    subscriptions = ()
                workspace, role = _workspace_shell(session, context)
            except ActionDenied as exc:
                if exc.code == "authentication_required":
                    return RedirectResponse(
                        "/login?return_to=/app/monitoring", status_code=303
                    )
                if exc.code == "workspace_required":
                    return RedirectResponse(
                        "/workspace/select?return_to=/app/monitoring", status_code=303
                    )
                return _html_error(request, exc)
        return templates.TemplateResponse(
            request=request,
            name="monitoring_feed.html",
            context={
                "principal": principal,
                "workspace": workspace,
                "role": role,
                "subscriptions": subscriptions,
                "entries": entries,
                "read_state": read_state,
                "severity_filter": severity_filter,
                "can_manage_monitoring": can_manage_monitoring,
                "monitoring_denial": monitoring_denial,
                "csrf": request.cookies.get(CSRF_COOKIE) or "",
            },
        )

    @app.post("/app/monitoring/feed/{entry_id}/read")
    async def monitoring_feed_read_form(request: Request, entry_id: UUID):
        form = await request.form()
        with _session_factory(request)() as session:
            try:
                principal = _require_principal(request, session)
                _verify_post_csrf(request, session, principal, str(form.get("csrf") or ""))
                if principal.active_workspace_id is None:
                    raise ActionDenied("workspace_required", "Выберите рабочее пространство.")
                mark_feed_entry_read(
                    session,
                    user_id=principal.user_id,
                    workspace_id=principal.active_workspace_id,
                    entry_id=entry_id,
                )
                session.commit()
            except ActionDenied as exc:
                session.rollback()
                return _html_error(request, exc)
        return RedirectResponse(
            safe_return_to(
                str(form.get("return_to") or ""),
                default="/app/monitoring",
            ),
            status_code=303,
        )

    @app.get("/app/api/companies/{inn}/monitoring")
    def api_monitoring_state(request: Request, inn: str):
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(
                    request, session, permission="company.view"
                )
                state = get_monitoring_state(
                    session,
                    user_id=principal.user_id,
                    workspace_id=context.workspace_id,
                    inn=inn,
                )
            except ActionDenied as exc:
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        return JSONResponse(_monitoring_payload(state))

    @app.post("/app/api/companies/{inn}/monitoring/{action}")
    def api_monitoring_action(request: Request, inn: str, action: str):
        if action not in {"enable", "pause", "resume"}:
            raise StarletteHTTPException(status_code=404)
        with _session_factory(request)() as session:
            try:
                principal = _require_principal(request, session)
                _verify_post_csrf(
                    request, session, principal, request.headers.get("x-csrf-token")
                )
                if principal.active_workspace_id is None:
                    raise ActionDenied("workspace_required", "Выберите рабочее пространство.")
                operations = {
                    "enable": subscribe_company,
                    "pause": pause_subscription,
                    "resume": resume_subscription,
                }
                _subscription, changed = operations[action](
                    session,
                    user_id=principal.user_id,
                    workspace_id=principal.active_workspace_id,
                    inn=inn,
                )
                state = get_monitoring_state(
                    session,
                    user_id=principal.user_id,
                    workspace_id=principal.active_workspace_id,
                    inn=inn,
                )
                session.commit()
            except ActionDenied as exc:
                session.rollback()
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        return JSONResponse(
            {"changed": changed, "monitoring": _monitoring_payload(state)},
            status_code=201 if action == "enable" and changed else 200,
        )

    @app.get("/app/api/monitoring")
    def api_monitoring_feed(
        request: Request,
        state: str = "all",
        severity: str = "",
    ):
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(
                    request, session, permission="monitoring.manage"
                )
                entries = list_workspace_feed(
                    session,
                    user_id=principal.user_id,
                    workspace_id=context.workspace_id,
                    read_state=state,
                    severity=severity or None,
                )
                subscriptions = list_workspace_subscriptions(
                    session,
                    user_id=principal.user_id,
                    workspace_id=context.workspace_id,
                )
            except ActionDenied as exc:
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        return JSONResponse(
            {
                "subscriptions": [
                    _subscription_payload(item) for item in subscriptions
                ],
                "items": [_feed_payload(item) for item in entries],
                "filters": {
                    "state": str(state or "all").lower(),
                    "severity": str(severity or "").upper() or None,
                },
            }
        )

    @app.post("/app/api/monitoring/feed/{entry_id}/read")
    def api_monitoring_feed_read(request: Request, entry_id: UUID):
        with _session_factory(request)() as session:
            try:
                principal = _require_principal(request, session)
                _verify_post_csrf(
                    request, session, principal, request.headers.get("x-csrf-token")
                )
                if principal.active_workspace_id is None:
                    raise ActionDenied("workspace_required", "Выберите рабочее пространство.")
                entry, changed = mark_feed_entry_read(
                    session,
                    user_id=principal.user_id,
                    workspace_id=principal.active_workspace_id,
                    entry_id=entry_id,
                )
                read_at = entry.read_at
                session.commit()
            except ActionDenied as exc:
                session.rollback()
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        return JSONResponse(
            {"id": str(entry_id), "changed": changed, "read_at": read_at.isoformat()}
        )

    @app.get("/app/api/context")
    def api_context(request: Request):
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(
                    request,
                    session,
                    permission="workspace.view",
                )
                workspace = session.get(Workspace, context.workspace_id)
                role = session.get(WorkspaceRole, context.role_id)
                if workspace is None or role is None:
                    raise ActionDenied(
                        "workspace_unavailable",
                        "Workspace is unavailable.",
                    )
            except ActionDenied as exc:
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        return JSONResponse(
            {
                "user": {"email": principal.email},
                "workspace": {"name": workspace.name, "role": role.role_key},
                "session": {"expires_at": principal.expires_at.isoformat()},
            }
        )

    @app.get("/app/api/dashboard")
    def api_dashboard(request: Request):
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(
                    request,
                    session,
                    permission="workspace.view",
                )
                dashboard = get_workspace_dashboard(
                    session,
                    user_id=principal.user_id,
                    workspace_id=context.workspace_id,
                )
            except ActionDenied as exc:
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        return JSONResponse(_dashboard_payload(dashboard))

    @app.get("/app/api/csrf")
    def api_csrf(request: Request):
        with _session_factory(request)() as session:
            try:
                _require_principal(request, session)
            except ActionDenied as exc:
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        return JSONResponse({"csrf_token": request.cookies.get(CSRF_COOKIE, "")})

    @app.post("/app/api/logout")
    def api_logout(request: Request):
        with _session_factory(request)() as session:
            try:
                principal = _require_principal(request, session)
                _verify_post_csrf(
                    request,
                    session,
                    principal,
                    request.headers.get("x-csrf-token"),
                )
                revoke_session(session, principal)
                record_logout_audit(
                    session,
                    user_id=principal.user_id,
                    workspace_id=principal.active_workspace_id,
                )
                session.commit()
            except ActionDenied as exc:
                session.rollback()
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        response = Response(status_code=204)
        _clear_session_cookies(response)
        return response

    @app.get("/app/api/saved-companies")
    def api_saved_companies(request: Request, q: str = ""):
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(
                    request,
                    session,
                    permission="company.view",
                )
                entries = saved_companies(
                    session,
                    user_id=principal.user_id,
                    workspace_id=context.workspace_id,
                    query=q,
                )
            except ActionDenied as exc:
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        return JSONResponse({"items": [_saved_payload(item) for item in entries]})

    @app.patch("/app/api/saved-companies/{saved_company_id}/note")
    async def api_saved_company_note(request: Request, saved_company_id: UUID):
        try:
            payload = await request.json()
        except (ValueError, TypeError):
            return JSONResponse(
                {"error": {"code": "invalid_request", "message": "Invalid JSON body"}},
                status_code=400,
            )
        if not isinstance(payload, dict) or "note" not in payload:
            return JSONResponse(
                {"error": {"code": "invalid_request", "message": "note is required"}},
                status_code=400,
            )
        if payload["note"] is not None and not isinstance(payload["note"], str):
            return JSONResponse(
                {"error": {"code": "invalid_request", "message": "note must be a string or null"}},
                status_code=422,
            )
        with _session_factory(request)() as session:
            try:
                principal = _require_principal(request, session)
                _verify_post_csrf(
                    request,
                    session,
                    principal,
                    request.headers.get("x-csrf-token"),
                )
                if principal.active_workspace_id is None:
                    raise ActionDenied("workspace_required", "Select a workspace.")
                entry, changed = update_saved_company_note(
                    session,
                    user_id=principal.user_id,
                    workspace_id=principal.active_workspace_id,
                    saved_company_id=saved_company_id,
                    note=payload["note"],
                )
                session.commit()
            except ActionDenied as exc:
                session.rollback()
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        return JSONResponse({"changed": changed, "item": _saved_payload(entry)})

    @app.post("/app/api/companies/{inn}/saved")
    def api_save_company(request: Request, inn: str):
        with _session_factory(request)() as session:
            try:
                principal = _require_principal(request, session)
                _verify_post_csrf(
                    request,
                    session,
                    principal,
                    request.headers.get("x-csrf-token"),
                )
                if principal.active_workspace_id is None:
                    raise ActionDenied("workspace_required", "Select a workspace.")
                created = save_company(
                    session,
                    user_id=principal.user_id,
                    workspace_id=principal.active_workspace_id,
                    inn=inn,
                )
                session.commit()
            except ActionDenied as exc:
                if exc.code == "quota_exceeded":
                    session.commit()
                else:
                    session.rollback()
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        return JSONResponse(
            {"saved": True, "created": created},
            status_code=201 if created else 200,
        )

    @app.delete("/app/api/companies/{inn}/saved")
    def api_unsave_company(request: Request, inn: str):
        with _session_factory(request)() as session:
            try:
                principal = _require_principal(request, session)
                _verify_post_csrf(
                    request,
                    session,
                    principal,
                    request.headers.get("x-csrf-token"),
                )
                if principal.active_workspace_id is None:
                    raise ActionDenied("workspace_required", "Select a workspace.")
                removed = unsave_company(
                    session,
                    user_id=principal.user_id,
                    workspace_id=principal.active_workspace_id,
                    inn=inn,
                )
                session.commit()
            except ActionDenied as exc:
                session.rollback()
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        return JSONResponse({"saved": False, "removed": removed})

    @app.get("/app/api/companies/{inn}")
    @app.get("/api/app/companies/{inn}")
    def company_api(request: Request, inn: str):
        if not valid_legal_inn(inn):
            raise StarletteHTTPException(status_code=404)
        projection = _public_repository(request).get_company(inn)
        if projection is None:
            raise StarletteHTTPException(status_code=404)
        with _session_factory(request)() as session:
            try:
                principal, _context = _require_active_workspace(
                    request, session, permission="company.view"
                )
                actions = _card_context(session, principal, projection)
            except ActionDenied as exc:
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        payload = projection.public_payload()
        payload["actions"] = actions
        return JSONResponse(payload)

    @app.get("/api/app/saved")
    def saved_api(request: Request, q: str = ""):
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(
                    request, session, permission="company.view"
                )
                entries = saved_companies(
                    session,
                    user_id=principal.user_id,
                    workspace_id=context.workspace_id,
                    query=q,
                )
            except ActionDenied as exc:
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        return JSONResponse({"items": [_saved_payload(item) for item in entries]})

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith(("/api/", "/app/api/")):
            return JSONResponse(
                {"error": {"code": "not_found", "message": "Not found"}},
                status_code=exc.status_code,
            )
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={
                "code": "not_found" if exc.status_code == 404 else "request_failed",
                "message": "Страница не найдена" if exc.status_code == 404 else "Не удалось выполнить запрос.",
            },
            status_code=exc.status_code,
        )

    return app


app = create_app()

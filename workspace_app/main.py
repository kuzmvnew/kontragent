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

from app.database.postgres import SessionLocal
from app.models.workspace import Workspace, WorkspaceRole
from public_app.contracts import valid_legal_inn
from public_app.repository import PublicRepository
from workspace_app.auth import (
    CSRF_COOKIE,
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
from workspace_app.service import (
    ActionDenied,
    authenticate_customer_attempt,
    authorize,
    action_state,
    is_saved,
    list_active_workspaces,
    record_login_audit,
    record_logout_audit,
    record_workspace_selection_audit,
    saved_companies,
    saved_count,
    save_company,
    unsave_company,
)


ROOT = Path(__file__).resolve().parent
def _shared_template_context(request: Request) -> dict:
    return {"public_origin": request.app.state.public_origin}


templates = Jinja2Templates(
    directory=str(ROOT / "templates"),
    context_processors=[_shared_template_context],
)
MAX_BODY_BYTES = 32_768


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
    saved = is_saved(
        session,
        user_id=principal.user_id,
        workspace_id=workspace_id,
        inn=projection.company.inn,
    )
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
    monitoring_allowed, monitoring_denial, _monitoring_limit = action_state(
        session,
        user_id=principal.user_id,
        workspace_id=workspace_id,
        permission_key="monitoring.manage",
    )
    if not saved:
        monitoring = {
            "state": "NOT_ACTIVE",
            "message": "Сначала сохраните компанию в этом Workspace.",
            "entry_enabled": False,
        }
    elif monitoring_allowed:
        monitoring = {
            "state": "NOT_IMPLEMENTED",
            "message": "Подписка на мониторинг появится в следующем продуктовом этапе.",
            "entry_enabled": True,
        }
    elif monitoring_denial == "permission_denied":
        monitoring = {
            "state": "NOT_ACTIVE",
            "message": "У вашей роли нет права управлять мониторингом.",
            "entry_enabled": True,
        }
    else:
        monitoring = {
            "state": "NOT_ACTIVE",
            "message": "Мониторинг не подключён для этого Workspace.",
            "entry_enabled": True,
        }
    return {
        "is_saved": saved,
        "can_save": can_save and (remaining is None or remaining > 0 or saved),
        "save_denial_reason": effective_save_denial,
        "save_denial_message": save_denial_message,
        "saved_limit": limit_value,
        "saved_used": used,
        "saved_remaining": remaining,
        "monitoring": monitoring,
    }


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
    app.mount(
        "/workspace-static",
        StaticFiles(directory=str(ROOT / "static")),
        name="workspace-static",
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable):
        length = request.headers.get("content-length")
        if length:
            try:
                if int(length) > MAX_BODY_BYTES:
                    return JSONResponse(
                        {"error": {"code": "request_too_large", "message": "Request too large"}},
                        status_code=413,
                    )
            except ValueError:
                return JSONResponse(
                    {"error": {"code": "invalid_request", "message": "Invalid request"}},
                    status_code=400,
                )
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

    @app.get("/app", response_class=HTMLResponse)
    def home(request: Request):
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(
                    request, session, permission="workspace.view"
                )
                workspace = session.get(Workspace, context.workspace_id)
                count = saved_count(
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
                "saved_count": count,
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
    def saved(request: Request):
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(
                    request, session, permission="company.view"
                )
                entries = saved_companies(
                    session,
                    user_id=principal.user_id,
                    workspace_id=context.workspace_id,
                )
                workspace, role = _workspace_shell(session, context)
                can_unsave, _unsave_denial, _unsave_limit = action_state(
                    session,
                    user_id=principal.user_id,
                    workspace_id=context.workspace_id,
                    permission_key="company.unsave",
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
                "can_unsave": can_unsave,
                "csrf": request.cookies.get(CSRF_COOKIE) or "",
            },
        )

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
    def api_saved_companies(request: Request):
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
                )
            except ActionDenied as exc:
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        return JSONResponse(
            {
                "items": [
                    {
                        "inn": item.inn,
                        "name": item.name,
                        "note": item.note,
                        "created_at": item.created_at.isoformat(),
                    }
                    for item in entries
                ]
            }
        )

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
    def saved_api(request: Request):
        with _session_factory(request)() as session:
            try:
                principal, context = _require_active_workspace(
                    request, session, permission="company.view"
                )
                entries = saved_companies(
                    session,
                    user_id=principal.user_id,
                    workspace_id=context.workspace_id,
                )
            except ActionDenied as exc:
                return JSONResponse(_error_payload(exc), status_code=exc.status_code)
        return JSONResponse(
            {
                "items": [
                    {
                        "inn": item.inn,
                        "name": item.name,
                        "note": item.note,
                        "created_at": item.created_at.isoformat(),
                    }
                    for item in entries
                ]
            }
        )

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

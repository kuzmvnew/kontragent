"""Loopback-only owner console for HOME DATA WORKER operations."""

from __future__ import annotations

import ipaddress
import json
import secrets
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from admin_app import service
from admin_app.auth import (
    LOGIN_LIMITER,
    SESSION_COOKIE,
    SESSIONS,
    load_auth_config,
    verify_password,
)
from admin_app.presentation import (
    action_label,
    category_label,
    format_date,
    format_datetime,
    owner_label,
    status_label,
)
from admin_app.system import (
    ADMIN_SERVICE,
    INCIDENT_SERVICE,
    PUBLIC_SYNC_SERVICE,
    WORKER_SERVICE,
    deployed_git_sha,
    invoke_fixed_helper,
    list_backups,
    storage_status,
    systemd_status,
)

ROOT = Path(__file__).resolve().parent
AUTH_CONFIG = load_auth_config()
templates = Jinja2Templates(directory=str(ROOT / "templates"))
templates.env.filters["json_pretty"] = lambda value: json.dumps(
    value, ensure_ascii=False, indent=2, default=str
)


def _filesize(value: int | None) -> str:
    if value is None:
        return "—"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


templates.env.filters["filesize"] = _filesize
templates.env.filters["datetime_ru"] = format_datetime
templates.env.filters["date_ru"] = format_date
templates.env.filters["status_ru"] = status_label
templates.env.filters["owner_ru"] = owner_label
templates.env.filters["category_ru"] = category_label
templates.env.filters["action_ru"] = action_label

app = FastAPI(
    title="NEXT Company Source Operations Console",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=list(AUTH_CONFIG.allowed_hosts),
)
app.mount("/admin/static", StaticFiles(directory=str(ROOT / "static")), name="admin-static")


@app.middleware("http")
async def loopback_and_security(request: Request, call_next):
    """Enforce the selected network boundary and owner session."""

    client_host = request.client.host if request.client else ""
    if AUTH_CONFIG.mode == "remote":
        if client_host not in AUTH_CONFIG.trusted_proxy_ips and client_host != "testclient":
            return JSONResponse({"detail": "trusted proxy required"}, status_code=403)
        if client_host != "testclient" and request.headers.get("x-forwarded-proto", "").lower() != "https":
            return JSONResponse({"detail": "HTTPS proxy required"}, status_code=403)
    else:
        try:
            loopback = ipaddress.ip_address(client_host).is_loopback
        except ValueError:
            # Starlette's in-process TestClient uses this sentinel only in tests.
            loopback = client_host == "testclient"
        if not loopback:
            return JSONResponse({"detail": "loopback access only"}, status_code=403)

    public_admin_path = (
        request.url.path in {"/admin/login", "/admin/health"}
        or request.url.path.startswith("/admin/static/")
    )
    claims = None
    if AUTH_CONFIG.auth_required:
        claims = SESSIONS.verify(request.cookies.get(SESSION_COOKIE), AUTH_CONFIG)
        if request.url.path.startswith("/admin/") and not public_admin_path and claims is None:
            if request.method in {"GET", "HEAD"}:
                return RedirectResponse("/admin/login", status_code=303)
            return JSONResponse({"detail": "authentication required"}, status_code=401)
    request.state.admin_authenticated = claims is not None or not AUTH_CONFIG.auth_required
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self'; img-src 'self' data:; "
        "script-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    )
    return response


def _render(request: Request, template: str, context: dict, *, status_code: int = 200):
    token = request.cookies.get("admin_csrf")
    new_token = not token or len(token) < 32
    if new_token:
        token = secrets.token_urlsafe(32)
    response = templates.TemplateResponse(
        request=request,
        name=template,
        context={
            "csrf_token": token,
            "admin_authenticated": getattr(request.state, "admin_authenticated", False),
            "admin_mode": AUTH_CONFIG.mode,
            **context,
        },
        status_code=status_code,
    )
    if new_token:
        response.set_cookie(
            "admin_csrf",
            token,
            httponly=True,
            samesite="strict",
            secure=AUTH_CONFIG.secure_cookie,
            path="/admin",
        )
    return response


async def _require_csrf(request: Request) -> dict:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise HTTPException(status_code=415, detail="form content type required")
    origin = request.headers.get("origin")
    # Chromium may serialize a same-page HTML form origin as ``null`` under the
    # console's strict no-referrer policy. The double-submit token and strict
    # SameSite cookie remain authoritative; concrete foreign origins are denied.
    if origin and origin != "null" and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(status_code=403, detail="origin mismatch")
    form = dict(await request.form())
    cookie = request.cookies.get("admin_csrf", "")
    submitted = str(form.get("_csrf", ""))
    if not cookie or not submitted or not secrets.compare_digest(cookie, submitted):
        raise HTTPException(status_code=403, detail="CSRF validation failed")
    return form


def _audit_auth(action: str, *, result: str) -> None:
    """Authentication must still work when audit storage is unavailable."""

    try:
        service.audit_action(
            action=action, source_id=None, job_id=None,
            previous_state={"mode": AUTH_CONFIG.mode},
            new_state={"authenticated": result == "success"}, result=result,
        )
    except Exception:
        pass


def _empty_snapshot() -> dict:
    return {
        "sources": [],
        "source_count": 0,
        "data_processes": 0,
        "connected": 0,
        "source_families": 0,
        "summary": {
            key: 0
            for key in (
                "operational", "first_run", "stale", "errors", "blocked",
                "disabled", "planned", "queue", "retry_scheduled", "active_leases",
            )
        },
        "master": {"total": 0, "legal": 0, "ip": 0},
        "enrichment": {
            "companies_not_started": 0,
            "companies_in_progress": 0,
            "companies_complete": 0,
            "risk_ready_companies": 0,
            "summary_ready_companies": 0,
            "public_ready_companies": 0,
            "coverage_at_least_1": 0,
            "coverage_at_least_3": 0,
            "coverage_at_least_5": 0,
            "coverage_at_least_10": 0,
            "coverage_100_percent": 0,
            "average_coverage_percent": 0.0,
            "median_coverage_percent": 0.0,
        },
        "incidents": {"open": 0, "running": 0, "waiting_source": 0, "review_required": 0, "recovered_today": 0, "exhausted": 0},
        "latest_run": None,
    }


def _empty_publication() -> dict:
    return {
        "site_ready": False,
        "site_error": "UNAVAILABLE",
        "active_release": None,
        "record_count": 0,
        "last_publication": None,
        "dirty_count": 0,
        "public_ready_count": 0,
        "enriching_count": 0,
        "status": "ОШИБКА",
        "last_error": None,
        "outbox": {"pending": 0, "coalesced": 0, "failed": 0, "published": 0},
    }


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/admin/sources", status_code=307)


@app.get("/admin/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request):
    if not AUTH_CONFIG.auth_required:
        return RedirectResponse("/admin/sources", status_code=303)
    if SESSIONS.verify(request.cookies.get(SESSION_COOKIE), AUTH_CONFIG):
        return RedirectResponse("/admin/sources", status_code=303)
    return _render(request, "login.html", {"error": None})


@app.post("/admin/login", response_class=HTMLResponse, include_in_schema=False)
async def login(request: Request):
    if not AUTH_CONFIG.auth_required:
        return RedirectResponse("/admin/sources", status_code=303)
    form = await _require_csrf(request)
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    key = f"{request.client.host if request.client else 'unknown'}:{username.casefold()[:100]}"
    valid = LOGIN_LIMITER.allowed(key) and verify_password(
        password, str(AUTH_CONFIG.password_hash)
    ) and secrets.compare_digest(username, str(AUTH_CONFIG.username))
    if not valid:
        LOGIN_LIMITER.failure(key)
        _audit_auth("login-failure", result="failed")
        return _render(
            request, "login.html",
            {"error": "Неверный логин или пароль. Повторите попытку позже."},
            status_code=401,
        )
    LOGIN_LIMITER.success(key)
    token = SESSIONS.create(AUTH_CONFIG)
    _audit_auth("login-success", result="success")
    response = RedirectResponse("/admin/sources", status_code=303)
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, secure=AUTH_CONFIG.secure_cookie,
        samesite="strict", max_age=AUTH_CONFIG.session_ttl_seconds,
        path="/admin",
    )
    return response


@app.post("/admin/logout", include_in_schema=False)
async def logout(request: Request):
    await _require_csrf(request)
    SESSIONS.revoke(request.cookies.get(SESSION_COOKIE), AUTH_CONFIG)
    _audit_auth("logout", result="success")
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/admin")
    return response


@app.get("/admin/health", include_in_schema=False)
async def health():
    database = service.postgres_health()
    return {
        "status": "ok" if database["available"] else "degraded",
        "database": database["status"],
    }


@app.get("/admin/sources", response_class=HTMLResponse, include_in_schema=False)
async def sources_page(
    request: Request, q: str = "", status: str = "", family: str = ""
):
    database = service.postgres_health()
    worker = systemd_status(WORKER_SERVICE)
    public_sync = systemd_status(PUBLIC_SYNC_SERVICE)
    try:
        snapshot = service.console_snapshot() if database["available"] else _empty_snapshot()
        publication = (
            service.public_publication_snapshot()
            if database["available"]
            else _empty_publication()
        )
    except Exception:
        database = {"available": False, "status": "UNAVAILABLE"}
        snapshot = _empty_snapshot()
        publication = _empty_publication()
    all_rows = list(snapshot["sources"])
    families = sorted({str(row["fact_family"]) for row in all_rows})
    snapshot["sources"] = service.filter_catalog_rows(
        all_rows, query=q, status=status, family=family
    )
    backups = list_backups(limit=1)
    return _render(
        request,
        "sources.html",
        {
            "snapshot": snapshot,
            "database": database,
            "worker": worker,
            "public_sync": public_sync,
            "publication": publication,
            "storage": storage_status(),
            "latest_backup": backups[0] if backups else None,
            "deployed_sha": deployed_git_sha(),
            "filters": {"q": q, "status": status, "family": family},
            "families": families,
            "filtered_count": len(snapshot["sources"]),
        },
    )


@app.get("/admin/publications", response_class=HTMLResponse, include_in_schema=False)
async def publications_page(request: Request):
    database = service.postgres_health()
    rows = service.public_publication_history(limit=100) if database["available"] else []
    publication = (
        service.public_publication_snapshot()
        if database["available"]
        else _empty_publication()
    )
    return _render(
        request,
        "publications.html",
        {
            "rows": rows,
            "publication": publication,
            "public_sync": systemd_status(PUBLIC_SYNC_SERVICE),
            "database": database,
        },
    )


@app.get("/admin/sources/{source_id}", response_class=HTMLResponse, include_in_schema=False)
async def source_page(request: Request, source_id: str):
    source = service.source_detail(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="source not found")
    return _render(request, "source_detail.html", {"source": source})


@app.get("/admin/sources/{source_id}/changes", response_class=HTMLResponse, include_in_schema=False)
async def changes_page(request: Request, source_id: str):
    rows = service.change_history(source_id)
    if rows is None:
        raise HTTPException(status_code=404, detail="source not found")
    return _render(
        request,
        "changes.html",
        {"source": service.source_detail(source_id), "changes": rows},
    )


@app.get("/admin/runs/{run_id}", response_class=HTMLResponse, include_in_schema=False)
async def run_page(request: Request, run_id: UUID):
    run = service.run_detail(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return _render(request, "run_detail.html", {"run": run})


@app.get("/admin/sources/{source_id}/confirm/{action}", response_class=HTMLResponse, include_in_schema=False)
async def confirm_source_action(request: Request, source_id: str, action: str):
    source = service.source_detail(source_id)
    if source is None or action not in service.ACTION_LABELS:
        raise HTTPException(status_code=404, detail="action not found")
    return _render(
        request,
        "confirm.html",
        {"source": source, "action": action, "action_label": service.ACTION_LABELS[action]},
    )


@app.post("/admin/sources/{source_id}/actions/{action}", include_in_schema=False)
async def source_action(request: Request, source_id: str, action: str):
    await _require_csrf(request)
    try:
        service.perform_source_action(source_id, action)
    except LookupError:
        raise HTTPException(status_code=404, detail="source not found")
    except Exception as error:
        return _render(
            request,
            "action_result.html",
            {
                "title": "Не удалось выполнить действие",
                "message": service.safe_error_message(error),
                "return_url": f"/admin/sources/{source_id}",
            },
            status_code=400,
        )
    return RedirectResponse(f"/admin/sources/{source_id}", status_code=303)


@app.post("/admin/sources/{source_id}/toggle", include_in_schema=False)
async def source_toggle(request: Request, source_id: str):
    form = await _require_csrf(request)
    requested = str(form.get("enabled", ""))
    if requested not in {"0", "1"}:
        raise HTTPException(status_code=400, detail="enabled must be 0 or 1")
    try:
        service.perform_source_action(
            source_id, "resume" if requested == "1" else "pause"
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="source not found")
    except Exception as error:
        return _render(
            request, "action_result.html",
            {
                "title": "Не удалось изменить источник",
                "message": service.safe_error_message(error),
                "return_url": "/admin/sources",
            },
            status_code=400,
        )
    return RedirectResponse("/admin/sources", status_code=303)


@app.get("/admin/audit", response_class=HTMLResponse, include_in_schema=False)
async def audit_page(request: Request):
    return _render(request, "audit.html", {"actions": service.audit_rows()})


@app.get("/admin/incidents", response_class=HTMLResponse, include_in_schema=False)
async def incidents_page(request: Request):
    return _render(request, "incidents.html", service.incident_rows())


@app.get("/admin/incidents/{incident_id}", response_class=HTMLResponse, include_in_schema=False)
async def incident_page(request: Request, incident_id: UUID):
    incident = service.incident_detail(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident not found")
    return _render(
        request,
        "incident_detail.html",
        {
            "incident": incident,
            "controller": systemd_status(INCIDENT_SERVICE),
            "incident_action_labels": service.INCIDENT_ACTION_LABELS,
        },
    )


@app.get("/admin/incidents/{incident_id}/confirm/{action}", response_class=HTMLResponse, include_in_schema=False)
async def confirm_incident_action(request: Request, incident_id: UUID, action: str):
    incident = service.incident_detail(incident_id)
    if (
        incident is None
        or action not in service.INCIDENT_ACTION_LABELS
        or action not in incident.get("available_actions", ())
    ):
        raise HTTPException(status_code=404, detail="action not found")
    return _render(
        request,
        "incident_confirm.html",
        {"incident": incident, "action": action, "action_label": service.INCIDENT_ACTION_LABELS[action]},
    )


@app.post("/admin/incidents/{incident_id}/actions/{action}", include_in_schema=False)
async def incident_action(request: Request, incident_id: UUID, action: str):
    await _require_csrf(request)
    try:
        result = service.perform_incident_action(incident_id, action)
    except LookupError:
        raise HTTPException(status_code=404, detail="incident not found")
    except Exception as error:
        return _render(
            request,
            "action_result.html",
            {"title": "Не удалось выполнить действие", "message": service.safe_error_message(error), "return_url": f"/admin/incidents/{incident_id}"},
            status_code=400,
        )
    if action == "check-source-now":
        source = service.source_detail(result["source_id"])
        return _render(
            request,
            "manual_recheck_result.html",
            {
                "incident": result,
                "source_name": source["source_name"] if source else result["source_id"],
                "controller": systemd_status(INCIDENT_SERVICE),
                "poll_seconds": 10,
            },
        )
    return RedirectResponse(f"/admin/incidents/{incident_id}", status_code=303)


@app.get("/admin/automation", response_class=HTMLResponse, include_in_schema=False)
async def automation_page(request: Request):
    return _render(request, "automation.html", service.automation_rows())


@app.get("/admin/automation/{source_id}/confirm", response_class=HTMLResponse, include_in_schema=False)
async def confirm_automation_policy(request: Request, source_id: str):
    context = service.automation_rows()
    policy = next((row for row in context["rows"] if row["source_id"] == source_id), None)
    if policy is None:
        raise HTTPException(status_code=404, detail="policy not found")
    return _render(request, "automation_confirm.html", {**context, "policy": policy})


@app.post("/admin/automation/{source_id}/update", include_in_schema=False)
async def automation_policy_update(request: Request, source_id: str):
    form = await _require_csrf(request)
    try:
        service.perform_policy_update(
            source_id=source_id,
            auto_heal_enabled=form.get("auto_heal") == "on",
            auto_code_repair_enabled=form.get("auto_code_repair") == "on",
            max_attempts=int(str(form.get("max_attempts", ""))),
            cooldown_seconds=int(str(form.get("cooldown_seconds", ""))),
        )
    except Exception as error:
        return _render(
            request,
            "action_result.html",
            {"title": "Не удалось обновить политику", "message": service.safe_error_message(error), "return_url": "/admin/automation"},
            status_code=400,
        )
    return RedirectResponse("/admin/automation", status_code=303)


def _system_context() -> dict:
    try:
        snapshot = service.console_snapshot()
    except Exception:
        snapshot = None
    return {
        "worker": systemd_status(WORKER_SERVICE),
        "admin_service": systemd_status(ADMIN_SERVICE),
        "incident_service": systemd_status(INCIDENT_SERVICE),
        "public_sync_service": systemd_status(PUBLIC_SYNC_SERVICE),
        "database": service.postgres_health(),
        "storage": storage_status(),
        "snapshot": snapshot,
        "deployed_sha": deployed_git_sha(),
    }


@app.get("/admin/system", response_class=HTMLResponse, include_in_schema=False)
async def system_page(request: Request):
    return _render(request, "system.html", _system_context())


@app.get("/admin/backups", response_class=HTMLResponse, include_in_schema=False)
async def backups_page(request: Request):
    return _render(request, "backups.html", {"backups": list_backups()})


@app.get("/admin/backups/confirm/create", response_class=HTMLResponse, include_in_schema=False)
async def confirm_backup(request: Request):
    return _render(
        request,
        "system_confirm.html",
        {"action": "create-backup", "title": "Создать резервную копию operational DB?", "return_url": "/admin/backups"},
    )


@app.post("/admin/backups/create", include_in_schema=False)
async def create_backup(request: Request):
    await _require_csrf(request)
    before = {"latest": list_backups(limit=1)}
    try:
        invoke_fixed_helper("create_backup")
        after = {"latest": list_backups(limit=1)}
        service.audit_action(action="create-backup", source_id=None, job_id=None, previous_state=before, new_state=after, result="success")
    except Exception as error:
        service.audit_action(action="create-backup", source_id=None, job_id=None, previous_state=before, new_state={"latest": list_backups(limit=1)}, result="failed", detail=str(error))
        return _render(request, "action_result.html", {"title": "Резервная копия не создана", "message": service.safe_error_message(error), "return_url": "/admin/backups"}, status_code=400)
    return RedirectResponse("/admin/backups", status_code=303)

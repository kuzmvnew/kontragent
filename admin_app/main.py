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
from admin_app.system import (
    ADMIN_SERVICE,
    WORKER_SERVICE,
    deployed_git_sha,
    invoke_fixed_helper,
    list_backups,
    storage_status,
    systemd_status,
)


ROOT = Path(__file__).resolve().parent
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

app = FastAPI(
    title="NEXT Company Source Operations Console",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["127.0.0.1", "localhost", "testserver"],
)
app.mount("/admin/static", StaticFiles(directory=str(ROOT / "static")), name="admin-static")


@app.middleware("http")
async def loopback_and_security(request: Request, call_next):
    """Deny non-loopback clients even after a deployment misconfiguration."""

    client_host = request.client.host if request.client else ""
    try:
        loopback = ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        # Starlette's in-process TestClient uses this sentinel only in tests.
        loopback = client_host == "testclient"
    if not loopback:
        return JSONResponse({"detail": "loopback access only"}, status_code=403)
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
        context={"csrf_token": token, **context},
        status_code=status_code,
    )
    if new_token:
        response.set_cookie(
            "admin_csrf",
            token,
            httponly=True,
            samesite="strict",
            secure=False,
            path="/admin",
        )
    return response


async def _require_csrf(request: Request) -> dict:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise HTTPException(status_code=415, detail="form content type required")
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(status_code=403, detail="origin mismatch")
    form = dict(await request.form())
    cookie = request.cookies.get("admin_csrf", "")
    submitted = str(form.get("_csrf", ""))
    if not cookie or not submitted or not secrets.compare_digest(cookie, submitted):
        raise HTTPException(status_code=403, detail="CSRF validation failed")
    return form


def _empty_snapshot() -> dict:
    return {
        "sources": [],
        "source_count": 0,
        "summary": {
            key: 0
            for key in (
                "operational", "first_run", "stale", "errors", "blocked",
                "disabled", "queue", "retry_scheduled", "active_leases",
            )
        },
        "master": {"total": 0, "legal": 0, "ip": 0},
        "latest_run": None,
    }


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/admin/sources", status_code=307)


@app.get("/admin/health", include_in_schema=False)
async def health():
    database = service.postgres_health()
    return {
        "status": "ok" if database["available"] else "degraded",
        "database": database["status"],
    }


@app.get("/admin/sources", response_class=HTMLResponse, include_in_schema=False)
async def sources_page(request: Request):
    database = service.postgres_health()
    worker = systemd_status(WORKER_SERVICE)
    try:
        snapshot = service.console_snapshot() if database["available"] else _empty_snapshot()
    except Exception:
        database = {"available": False, "status": "UNAVAILABLE"}
        snapshot = _empty_snapshot()
    backups = list_backups(limit=1)
    return _render(
        request,
        "sources.html",
        {
            "snapshot": snapshot,
            "database": database,
            "worker": worker,
            "storage": storage_status(),
            "latest_backup": backups[0] if backups else None,
            "deployed_sha": deployed_git_sha(),
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
                "title": "Действие не выполнено",
                "message": service.safe_error_message(error),
                "return_url": f"/admin/sources/{source_id}",
            },
            status_code=400,
        )
    return RedirectResponse(f"/admin/sources/{source_id}", status_code=303)


@app.get("/admin/audit", response_class=HTMLResponse, include_in_schema=False)
async def audit_page(request: Request):
    return _render(request, "audit.html", {"actions": service.audit_rows()})


def _system_context() -> dict:
    try:
        snapshot = service.console_snapshot()
    except Exception:
        snapshot = None
    return {
        "worker": systemd_status(WORKER_SERVICE),
        "admin_service": systemd_status(ADMIN_SERVICE),
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
        return _render(request, "action_result.html", {"title": "Backup не создан", "message": service.safe_error_message(error), "return_url": "/admin/backups"}, status_code=400)
    return RedirectResponse("/admin/backups", status_code=303)

"""Public, read-only FastAPI entry point for nextcompany.pro.

This package deliberately imports no operational application modules.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from public_app.contracts import PublicProjection, valid_legal_inn
from public_app.repository import PublicRepository


ROOT = Path(__file__).resolve().parent
PUBLIC_ORIGIN = os.getenv("PUBLIC_ORIGIN", "https://nextcompany.pro").rstrip("/")
MAX_QUERY_LENGTH = 160
MAX_BODY_BYTES = 16_384

templates = Jinja2Templates(directory=str(ROOT / "templates"))


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _public_date(value) -> str:
    if value in (None, ""):
        return "—"
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    try:
        return date.fromisoformat(str(value)[:10]).strftime("%d.%m.%Y")
    except ValueError:
        return str(value)


def _public_number(value) -> str:
    if value in (None, ""):
        return "—"
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    rendered = f"{number:,.2f}" if number != number.to_integral() else f"{number:,.0f}"
    return rendered.replace(",", " ").replace(".", ",")


def _public_period(value) -> str:
    text = str(value or "")
    if text.startswith("YEAR:"):
        return text.removeprefix("YEAR:")
    if text.startswith("DATE:"):
        return _public_date(text.removeprefix("DATE:"))
    if text.startswith("QUARTER:"):
        year, quarter = text.removeprefix("QUARTER:").split(":", 1)
        return f"{quarter}, {year}"
    return text or "—"


templates.env.filters["public_date"] = _public_date
templates.env.filters["public_number"] = _public_number
templates.env.filters["public_period"] = _public_period


def _repo(request: Request):
    return request.app.state.repository


def _validated_workspace_origin(value: str | None) -> str:
    """Return one explicit, safe origin for the customer Workspace boundary.

    An empty value means that deployment routes the public and Workspace apps
    on the same origin. A configured value may contain only an HTTP(S) origin;
    paths, credentials, query strings and fragments are rejected.
    """

    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return ""
    parsed = urlsplit(raw)
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("WORKSPACE_ORIGIN contains an invalid port") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("WORKSPACE_ORIGIN must be an HTTP(S) origin without a path")
    return f"{parsed.scheme}://{parsed.netloc}"


def _workspace_url(request: Request, path: str, *, return_to: str | None = None) -> str:
    origin = request.app.state.workspace_origin
    query = urlencode({"return_to": return_to}) if return_to else ""
    return f"{origin}{path}{'?' + query if query else ''}"


def _page_context(request: Request, **values) -> dict:
    return {
        "demo_mode": request.app.state.demo_mode,
        "local_real_preview": request.app.state.local_real_preview,
        "workspace_login_url": _workspace_url(
            request,
            "/login",
            return_to="/app",
        ),
        **values,
    }


def _card_description(projection: PublicProjection) -> str:
    company = projection.company
    parts = [company.name, f"ИНН {company.inn}"]
    if company.legal_status:
        parts.append(company.legal_status)
    if company.address:
        parts.append(company.address)
    return ". ".join(parts)[:300]


def _card_json_ld(projection: PublicProjection) -> dict:
    company = projection.company
    value: dict = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": company.full_name or company.name,
        "identifier": company.inn,
        "url": f"{PUBLIC_ORIGIN}/companies/{company.inn}",
    }
    if company.address:
        value["address"] = company.address
    if company.registration_date:
        value["foundingDate"] = company.registration_date.isoformat()
    return value


def create_app(repository=None, *, workspace_origin: str | None = None) -> FastAPI:
    demo_mode = _env_flag("NEXTCOMPANY_DEMO_MODE")
    local_real_preview = _env_flag("NEXTCOMPANY_LOCAL_REAL_PREVIEW")
    if demo_mode and local_real_preview:
        raise ValueError("demo mode and local real-data preview are mutually exclusive")
    force_noindex = _env_flag("PUBLIC_FORCE_NOINDEX") or demo_mode or local_real_preview
    app = FastAPI(
        title="NEXT Company Public",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.repository = repository or PublicRepository()
    app.state.force_noindex = force_noindex
    app.state.demo_mode = demo_mode
    app.state.local_real_preview = local_real_preview
    app.state.workspace_origin = _validated_workspace_origin(
        os.getenv("WORKSPACE_ORIGIN", "")
        if workspace_origin is None
        else workspace_origin
    )
    allowed_hosts = [
        host.strip()
        for host in os.getenv(
            "PUBLIC_TRUSTED_HOSTS",
            "nextcompany.pro,www.nextcompany.pro,localhost,127.0.0.1,testserver",
        ).split(",")
        if host.strip()
    ]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

    @app.middleware("http")
    async def public_security(request: Request, call_next: Callable):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_BODY_BYTES:
                    return PlainTextResponse("Request too large", status_code=413)
            except ValueError:
                return PlainTextResponse("Invalid request", status_code=400)
        try:
            response = await call_next(request)
        except Exception:
            if request.url.path.startswith("/api/"):
                response = JSONResponse({"detail": "Service unavailable"}, status_code=500)
            else:
                response = templates.TemplateResponse(
                    request=request,
                    name="error.html",
                    context=_page_context(
                        request,
                        status_code=500,
                        message="Сервис временно недоступен",
                    ),
                    status_code=500,
                )
        response.headers.update(
            {
                "Content-Security-Policy": (
                    "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                    "script-src 'self'; base-uri 'self'; form-action 'self'; "
                    "frame-ancestors 'none'; object-src 'none'"
                ),
                "Referrer-Policy": "strict-origin-when-cross-origin",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
                "Cross-Origin-Opener-Policy": "same-origin",
            }
        )
        if force_noindex:
            response.headers["X-Robots-Tag"] = "noindex, nofollow, nosnippet"
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path.startswith("/api/"):
            response.headers["X-Robots-Tag"] = "noindex, nofollow, nosnippet"
            response.headers["Cache-Control"] = "no-store"
        elif response.status_code >= 400:
            response.headers["X-Robots-Tag"] = "noindex, follow"
            response.headers["Cache-Control"] = "no-store"
        else:
            response.headers.setdefault("Cache-Control", "public, max-age=60, stale-while-revalidate=300")
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Not found" if exc.status_code == 404 else "Request failed"}, status_code=exc.status_code)
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context=_page_context(
                request,
                status_code=exc.status_code,
                message=(
                    "Страница не найдена"
                    if exc.status_code == 404
                    else "Не удалось выполнить запрос"
                ),
            ),
            status_code=exc.status_code,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, _exc: RequestValidationError):
        return PlainTextResponse("Invalid request", status_code=400)

    @app.get("/", response_class=HTMLResponse)
    def landing(request: Request):
        ready, release_id, count = _repo(request).ready()
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context=_page_context(
                request,
                ready=ready,
                release_id=release_id,
                company_count=count,
                origin=PUBLIC_ORIGIN,
            ),
        )

    @app.get("/search", response_class=HTMLResponse)
    def search(request: Request, q: str = ""):
        query = " ".join(q.split())[:MAX_QUERY_LENGTH]
        if re.fullmatch(r"\d{10}", query) and valid_legal_inn(query):
            if _repo(request).get_company(query):
                return RedirectResponse(f"/companies/{query}", status_code=308)
        results = _repo(request).search(query) if query else []
        return templates.TemplateResponse(
            request=request,
            name="search.html",
            context=_page_context(
                request,
                query=query,
                results=results,
                origin=PUBLIC_ORIGIN,
            ),
            headers={"X-Robots-Tag": "noindex, follow"},
        )

    @app.get("/companies/{inn}", response_class=HTMLResponse)
    def company_card(request: Request, inn: str):
        if not valid_legal_inn(inn):
            raise StarletteHTTPException(status_code=404)
        projection = _repo(request).get_company(inn)
        if projection is None:
            raise StarletteHTTPException(status_code=404)
        robots = (
            "noindex, nofollow"
            if force_noindex
            else (
                "index, follow"
                if projection.publication.index_eligible
                else "noindex, follow"
            )
        )
        return templates.TemplateResponse(
            request=request,
            name="company.html",
            context=_page_context(
                request,
                projection=projection,
                canonical=f"{PUBLIC_ORIGIN}/companies/{inn}",
                description=_card_description(projection),
                json_ld=_card_json_ld(projection),
                robots=robots,
                workspace_company_url=_workspace_url(
                    request,
                    "/login",
                    return_to=f"/app/companies/{inn}",
                ),
            ),
            headers={"X-Robots-Tag": robots},
        )

    @app.get("/company/{inn}")
    def legacy_company(inn: str):
        return RedirectResponse(f"/companies/{inn}", status_code=308)

    @app.get("/api/company/{inn}")
    def company_api(request: Request, inn: str):
        if not valid_legal_inn(inn):
            raise StarletteHTTPException(status_code=404)
        projection = _repo(request).get_company(inn)
        if projection is None:
            raise StarletteHTTPException(status_code=404)
        return JSONResponse(projection.public_payload())

    @app.get("/robots.txt")
    def robots():
        if force_noindex:
            return PlainTextResponse("User-agent: *\nDisallow: /\n")
        body = "\n".join(
            (
                "User-agent: *",
                "Disallow: /search",
                "Disallow: /api/",
                "Disallow: /internal/",
                "Disallow: /docs",
                "Disallow: /redoc",
                "Disallow: /openapi.json",
                f"Sitemap: {PUBLIC_ORIGIN}/sitemap.xml",
                "",
            )
        )
        return PlainTextResponse(body)

    @app.get("/sitemap.xml")
    def sitemap(request: Request):
        rows = [] if force_noindex else _repo(request).sitemap_rows()
        return templates.TemplateResponse(
            request=request,
            name="sitemap.xml",
            context={"rows": rows, "origin": PUBLIC_ORIGIN},
            media_type="application/xml",
        )

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/ready")
    def ready(request: Request):
        is_ready, release_id, count = _repo(request).ready()
        return JSONResponse(
            {"status": "ready" if is_ready else "not_ready", "release_id": release_id, "record_count": count},
            status_code=200 if is_ready else 503,
        )

    return app


app = create_app()

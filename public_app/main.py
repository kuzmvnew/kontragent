"""Public, read-only FastAPI entry point for nextcompany.pro.

This package deliberately imports no operational application modules.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable

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


def _repo(request: Request):
    return request.app.state.repository


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


def create_app(repository=None) -> FastAPI:
    app = FastAPI(
        title="NEXT Company Public",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.repository = repository or PublicRepository()
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
                    context={"status_code": 500, "message": "Сервис временно недоступен"},
                    status_code=500,
                )
        response.headers.update(
            {
                "Content-Security-Policy": (
                    "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                    "script-src 'none'; base-uri 'self'; form-action 'self'; "
                    "frame-ancestors 'none'; object-src 'none'"
                ),
                "Referrer-Policy": "strict-origin-when-cross-origin",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
                "Cross-Origin-Opener-Policy": "same-origin",
            }
        )
        if request.url.path.startswith("/api/"):
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
            context={
                "status_code": exc.status_code,
                "message": "Страница не найдена" if exc.status_code == 404 else "Не удалось выполнить запрос",
            },
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
            context={"ready": ready, "release_id": release_id, "company_count": count, "origin": PUBLIC_ORIGIN},
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
            context={"query": query, "results": results, "origin": PUBLIC_ORIGIN},
            headers={"X-Robots-Tag": "noindex, follow"},
        )

    @app.get("/companies/{inn}", response_class=HTMLResponse)
    def company_card(request: Request, inn: str):
        if not valid_legal_inn(inn):
            raise StarletteHTTPException(status_code=404)
        projection = _repo(request).get_company(inn)
        if projection is None:
            raise StarletteHTTPException(status_code=404)
        robots = "index, follow" if projection.publication.index_eligible else "noindex, follow"
        return templates.TemplateResponse(
            request=request,
            name="company.html",
            context={
                "projection": projection,
                "canonical": f"{PUBLIC_ORIGIN}/companies/{inn}",
                "description": _card_description(projection),
                "json_ld": _card_json_ld(projection),
                "robots": robots,
            },
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
        return JSONResponse(projection.model_dump(mode="json"))

    @app.get("/robots.txt")
    def robots():
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
        rows = _repo(request).sitemap_rows()
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

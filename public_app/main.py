"""Public, read-only FastAPI entry point for nextcompany.pro.

This package deliberately imports no operational application modules.
"""

from __future__ import annotations

import os
import re
import gzip
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from public_app.contracts import valid_legal_inn
from public_app.repository import PublicRepository
from public_app.seo import (
    COMPANY_SHARD_COUNT,
    build_catalog_pagination,
    catalog_page_url,
    compile_seo_projection,
    sitemap_shard,
)


ROOT = Path(__file__).resolve().parent
PUBLIC_ORIGIN = os.getenv("PUBLIC_ORIGIN", "https://nextcompany.pro").rstrip("/")
MAX_QUERY_LENGTH = 160
MAX_BODY_BYTES = 16_384

templates = Jinja2Templates(directory=str(ROOT / "templates"))


def _repo(request: Request):
    return request.app.state.repository


TRACKING_PARAMETERS = {
    "gclid",
    "yclid",
    "fbclid",
    "_openstat",
}


def _tracking_only(request: Request) -> bool:
    keys = tuple(request.query_params.keys())
    return bool(keys) and all(key.casefold().startswith("utm_") or key.casefold() in TRACKING_PARAMETERS for key in keys)


def _sitemap_rows(repository, shard: str) -> list[dict]:
    try:
        return repository.sitemap_rows(shard=shard)
    except TypeError:  # Compatibility with the v1 repository/test doubles.
        item = getattr(repository, "item", None)
        if item is not None and not compile_seo_projection(item).sitemap_eligible:
            return []
        return [row for row in repository.sitemap_rows() if sitemap_shard(str(row["inn"])) == shard]


def _catalog_page(repository, page: int, page_size: int = 24):
    if hasattr(repository, "catalog_page"):
        return repository.catalog_page(page=page, page_size=page_size)
    item = getattr(repository, "item", None)
    eligible = item is not None and compile_seo_projection(item).catalog_eligible
    return ([item] if eligible and page == 1 else []), (1 if eligible else 0)


def create_app(repository=None) -> FastAPI:
    app = FastAPI(
        title="NEXT Company Public",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
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
        target = urlsplit(PUBLIC_ORIGIN)
        request_host = request.headers.get("host", "").split(":", 1)[0].casefold()
        canonical_host = (target.hostname or "").casefold()
        forwarded_scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",", 1)[0].strip()
        if request_host in {canonical_host, f"www.{canonical_host}"}:
            normalized_path = request.url.path
            legacy = re.fullmatch(r"/company/(\d{10})/?", normalized_path)
            trailing = re.fullmatch(r"/companies/(\d{10})/", normalized_path)
            if legacy and valid_legal_inn(legacy.group(1)):
                normalized_path = f"/companies/{legacy.group(1)}"
            elif trailing and valid_legal_inn(trailing.group(1)):
                normalized_path = f"/companies/{trailing.group(1)}"
            strip_query = _tracking_only(request)
            needs_redirect = (
                request_host != canonical_host
                or forwarded_scheme != target.scheme
                or normalized_path != request.url.path
                or strip_query
            )
            if needs_redirect:
                query = "" if strip_query else request.url.query
                location = f"{PUBLIC_ORIGIN}{normalized_path}"
                if query:
                    location += f"?{query}"
                return RedirectResponse(location, status_code=308)
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

    @app.get("/companies", response_class=HTMLResponse)
    def companies_catalog(request: Request):
        return _render_catalog(request, 1)

    @app.get("/companies/page/1")
    def companies_page_one():
        return RedirectResponse("/companies", status_code=308)

    @app.get("/companies/page/{page}", response_class=HTMLResponse)
    def companies_catalog_page(request: Request, page: int):
        if page < 2:
            raise StarletteHTTPException(status_code=404)
        return _render_catalog(request, page)

    def _render_catalog(request: Request, page: int):
        items, total = _catalog_page(_repo(request), page)
        if not items or total <= 0:
            raise StarletteHTTPException(status_code=404)
        try:
            pagination = build_catalog_pagination(page, total)
        except ValueError as exc:
            raise StarletteHTTPException(status_code=404) from exc
        has_variant = bool(request.query_params)
        robots = "noindex, follow" if has_variant else "index, follow"
        canonical_path = catalog_page_url(page)
        return templates.TemplateResponse(
            request=request,
            name="catalog.html",
            context={
                "items": items,
                "pagination": pagination,
                "canonical": f"{PUBLIC_ORIGIN}{canonical_path}",
                "robots": robots,
            },
            headers={"X-Robots-Tag": robots},
        )

    @app.get("/companies/{inn}", response_class=HTMLResponse)
    def company_card(request: Request, inn: str):
        if not valid_legal_inn(inn):
            raise StarletteHTTPException(status_code=404)
        projection = _repo(request).get_company(inn)
        if projection is None:
            raise StarletteHTTPException(status_code=404)
        if _tracking_only(request):
            return RedirectResponse(f"/companies/{inn}", status_code=308)
        stored_seo = _repo(request).get_seo_projection(inn) if hasattr(_repo(request), "get_seo_projection") else None
        seo = stored_seo or compile_seo_projection(projection)
        robots = "noindex, follow" if request.query_params else seo.robots
        return templates.TemplateResponse(
            request=request,
            name="company.html",
            context={
                "projection": projection,
                "seo": seo,
                "canonical": seo.canonical_url,
                "description": seo.metadata.description,
                "json_ld": seo.json_ld,
                "robots": robots,
                "stale_adverse": bool(projection.risk.factors)
                and any(source.freshness.value != "CURRENT" for source in projection.sources),
            },
            headers={"X-Robots-Tag": robots},
        )

    @app.get("/companies/{inn}/")
    def company_card_trailing(inn: str):
        if not valid_legal_inn(inn):
            raise StarletteHTTPException(status_code=404)
        return RedirectResponse(f"/companies/{inn}", status_code=308)

    @app.get("/company/{inn}")
    def legacy_company(inn: str):
        if not valid_legal_inn(inn):
            raise StarletteHTTPException(status_code=404)
        return RedirectResponse(f"/companies/{inn}", status_code=308)

    @app.get("/company/{inn}/")
    def legacy_company_trailing(inn: str):
        if not valid_legal_inn(inn):
            raise StarletteHTTPException(status_code=404)
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
        body = "\n".join(
            (
                "User-agent: *",
                "Allow: /",
                "Disallow: /internal/",
                "Disallow: /admin/",
                "Disallow: /docs",
                "Disallow: /openapi.json",
                f"Sitemap: {PUBLIC_ORIGIN}/sitemap.xml",
                "",
            )
        )
        return PlainTextResponse(body)

    @app.get("/sitemap.xml")
    def sitemap_index(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="sitemap_index.xml",
            context={"shards": tuple(f"0{value:x}" for value in range(COMPANY_SHARD_COUNT)), "origin": PUBLIC_ORIGIN},
            media_type="application/xml",
        )

    @app.get("/sitemaps/static.xml.gz")
    def static_sitemap(request: Request):
        rendered = templates.get_template("sitemap_static.xml").render(origin=PUBLIC_ORIGIN)
        return Response(
            gzip.compress(rendered.encode("utf-8"), mtime=0),
            media_type="application/xml",
            headers={"Content-Encoding": "gzip"},
        )

    @app.get("/sitemaps/companies-{shard}.xml.gz")
    def company_sitemap(request: Request, shard: str):
        if not re.fullmatch(r"0[0-9a-f]", shard):
            raise StarletteHTTPException(status_code=404)
        rows = _sitemap_rows(_repo(request), shard[1])
        rendered = templates.get_template("sitemap.xml").render(rows=rows, origin=PUBLIC_ORIGIN)
        return Response(
            gzip.compress(rendered.encode("utf-8"), mtime=0),
            media_type="application/xml",
            headers={"Content-Encoding": "gzip"},
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

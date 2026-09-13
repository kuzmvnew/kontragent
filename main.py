from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.services.company_service import (
    get_company_by_inn,
    search_companies,
)


app = FastAPI(
    title="Контрагент",
    description="Сервис проверки российских компаний",
    version="0.3.0",
)


app.mount(
    "/static",
    StaticFiles(directory="static"),
    name="static",
)


templates = Jinja2Templates(
    directory="templates",
)


@app.get(
    "/",
    response_class=HTMLResponse,
)
def home(request: Request):

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "query": "",
            "results": [],
            "error": None,
        },
    )


@app.get(
    "/search",
    response_class=HTMLResponse,
)
def search(
    request: Request,
    q: str = "",
):

    query = q.strip()

    if not query:

        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "query": "",
                "results": [],
                "error": (
                    "Введите название компании или ИНН"
                ),
            },
        )

    # Если пользователь ввёл полный ИНН,
    # сразу открываем карточку компании.
    if query.isdigit() and len(query) in (10, 12):

        company = get_company_by_inn(
            query
        )

        if company is not None:

            return templates.TemplateResponse(
                request=request,
                name="company.html",
                context={
                    "company": company,
                },
            )

    # Если введено название или часть ИНН,
    # показываем список результатов.
    results = search_companies(
        query=query,
        limit=30,
    )

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "query": query,
            "results": results,
            "error": (
                None
                if results
                else "Компании не найдены"
            ),
        },
    )


@app.get(
    "/company/{inn}",
    response_class=HTMLResponse,
)
def company_page(
    request: Request,
    inn: str,
):

    if not inn.isdigit():

        raise HTTPException(
            status_code=400,
            detail=(
                "ИНН должен содержать "
                "только цифры"
            ),
        )

    if len(inn) not in (10, 12):

        raise HTTPException(
            status_code=400,
            detail=(
                "ИНН должен содержать "
                "10 или 12 цифр"
            ),
        )

    company = get_company_by_inn(
        inn
    )

    if company is None:

        raise HTTPException(
            status_code=404,
            detail="Компания не найдена",
        )

    return templates.TemplateResponse(
        request=request,
        name="company.html",
        context={
            "company": company,
        },
    )


@app.get("/api/company/{inn}")
def company_api(inn: str):

    if not inn.isdigit():

        raise HTTPException(
            status_code=400,
            detail=(
                "ИНН должен содержать "
                "только цифры"
            ),
        )

    if len(inn) not in (10, 12):

        raise HTTPException(
            status_code=400,
            detail=(
                "ИНН должен содержать "
                "10 или 12 цифр"
            ),
        )

    company = get_company_by_inn(
        inn
    )

    if company is None:

        raise HTTPException(
            status_code=404,
            detail="Компания не найдена",
        )

    return company


@app.get("/api/search")
def search_api(
    q: str,
    limit: int = 20,
):

    if limit < 1:
        limit = 1

    if limit > 100:
        limit = 100

    return {
        "query": q,
        "results": search_companies(
            query=q,
            limit=limit,
        ),
    }


@app.get("/api/health")
def health():

    return {
        "status": "ok",
        "service": "kontragent",
    }
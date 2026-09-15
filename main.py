from fastapi import (
    FastAPI,
    HTTPException,
    Request,
)
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.aggregators.company_product_aggregator import (
    get_company_for_web,
)
from app.services.company_service import (
    search_companies,
)


# =========================================================
# FASTAPI
# =========================================================


app = FastAPI(
    title="Проверка контрагентов",
    description=(
        "Сервис проверки российских "
        "компаний и индивидуальных "
        "предпринимателей"
    ),
    version="0.5.0",
)


# =========================================================
# STATIC FILES
# =========================================================


app.mount(
    "/static",
    StaticFiles(
        directory="static",
    ),
    name="static",
)


# =========================================================
# TEMPLATES
# =========================================================


templates = Jinja2Templates(
    directory="templates",
)


# =========================================================
# JINJA FILTERS
# =========================================================


def format_number(value):
    """
    Форматирует большие числа:

    11469690000
        ↓
    11 469 690 000
    """

    if value is None:
        return "—"

    try:
        number = int(value)

        return (
            f"{number:,}"
            .replace(
                ",",
                " ",
            )
        )

    except (
        TypeError,
        ValueError,
    ):
        return str(value)


templates.env.filters[
    "number"
] = format_number


# =========================================================
# HELPERS
# =========================================================


def normalize_inn(
    value: str,
):
    """
    Оставляем в ИНН только цифры.

    Юридическое лицо:
    10 цифр

    ИП:
    12 цифр
    """

    return "".join(
        symbol
        for symbol in str(value)
        if symbol.isdigit()
    )


def is_full_inn(
    value: str,
):
    inn = normalize_inn(
        value
    )

    return len(inn) in (
        10,
        12,
    )


def prepare_company_for_template(
    company,
):
    """
    Подготавливает карточку
    для HTML-шаблона.

    Настоящий Risk Engine
    мы добавим позже.

    Поэтому сейчас НЕ создаём
    фальшивую оценку 0/100.
    """

    if company is None:
        return None

    result = dict(
        company
    )

    result.setdefault(
        "risk_score",
        None,
    )

    result.setdefault(
        "risk_level",
        None,
    )

    result.setdefault(
        "risk_label",
        "Оценка пока не рассчитана",
    )

    result.setdefault(
        "risk_factors",
        [],
    )

    result.setdefault(
        "phones",
        [],
    )

    result.setdefault(
        "emails",
        [],
    )

    result.setdefault(
        "websites",
        [],
    )

    result.setdefault(
        "branches",
        [],
    )

    return result


# =========================================================
# HOME
# =========================================================


@app.get(
    "/",
    response_class=HTMLResponse,
)
async def home(
    request: Request,
):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "query": "",
            "results": None,
        },
    )


# =========================================================
# SEARCH
# =========================================================


@app.get(
    "/search",
    response_class=HTMLResponse,
)
async def search(
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
            },
        )

    # -----------------------------------------------------
    # Если введён полный ИНН,
    # отправляем пользователя
    # на постоянную страницу компании.
    # -----------------------------------------------------

    if is_full_inn(
        query
    ):
        inn = normalize_inn(
            query
        )

        return RedirectResponse(
            url=f"/company/{inn}",
            status_code=302,
        )

    # -----------------------------------------------------
    # Поиск по названию
    # или части ИНН.
    #
    # Здесь внешний API
    # вообще не используется.
    # -----------------------------------------------------

    results = search_companies(
        query=query,
        limit=20,
    )

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "query": query,
            "results": results,
        },
    )


# =========================================================
# COMPANY PAGE
# =========================================================


@app.get(
    "/company/{inn}",
    response_class=HTMLResponse,
)
async def company_page(
    request: Request,
    inn: str,
):
    clean_inn = normalize_inn(
        inn
    )

    if len(
        clean_inn
    ) not in (
        10,
        12,
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "ИНН должен содержать "
                "10 или 12 цифр"
            ),
        )

    # -----------------------------------------------------
    # ВАЖНО:
    #
    # Здесь теперь работает
    # наш Aggregator.
    #
    # Для существующей компании:
    #
    # PostgreSQL
    # + cached source snapshots
    #
    # без нового обращения
    # к DaData.
    #
    # Если компании вообще нет,
    # Aggregator может попробовать
    # внешний источник.
    # -----------------------------------------------------

    company = get_company_for_web(
        clean_inn
    )

    if company is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "Компания не найдена"
            ),
        )

    company = (
        prepare_company_for_template(
            company
        )
    )

    return templates.TemplateResponse(
        request=request,
        name="company.html",
        context={
            "company": company,
        },
    )


# =========================================================
# API: COMPANY
# =========================================================


@app.get(
    "/api/company/{inn}",
)
async def api_company(
    inn: str,
):
    clean_inn = normalize_inn(
        inn
    )

    if len(
        clean_inn
    ) not in (
        10,
        12,
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "ИНН должен содержать "
                "10 или 12 цифр"
            ),
        )

    company = get_company_for_web(
        clean_inn
    )

    if company is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "Компания не найдена"
            ),
        )

    return company


# =========================================================
# API: SEARCH
# =========================================================


@app.get(
    "/api/search",
)
async def api_search(
    q: str = "",
):
    query = q.strip()

    if not query:
        return {
            "query": "",
            "count": 0,
            "results": [],
        }

    results = search_companies(
        query=query,
        limit=20,
    )

    return {
        "query": query,
        "count": len(
            results
        ),
        "results": results,
    }


# =========================================================
# HEALTH
# =========================================================


@app.get(
    "/api/health",
)
async def health():
    return {
        "status": "ok",
        "version": "0.5.0",
        "database": "postgresql",
        "aggregator": True,
    }

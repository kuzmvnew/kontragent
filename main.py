from datetime import date, datetime

from fastapi import (
    FastAPI,
    HTTPException,
    Query,
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
from app.services.cbr_finorg_service import (
    refresh_cbr_finorg_check_for_inn,
)
from app.services.corporate_disclosure_service import refresh_corporate_disclosure_check
from app.services.arbitration_court_service import refresh_arbitration_court_check
from app.services.general_court_service import refresh_general_court_check
from app.services.protected_source_session_service import start_protected_source_session
from app.services.roszdrav_service import (
    refresh_roszdrav_medical_device_check,
    refresh_roszdrav_unified_license_check,
)
from app.services.roskomnadzor_service import refresh_pd_operator_check
from app.services.nopriz_service import refresh_nopriz_check
from app.services.nostroy_service import refresh_nostroy_check
from app.services.company_service import (
    search_companies,
)
from app.services.npd_service import (
    refresh_npd_check_for_inn,
)
from app.services.data_readiness_service import get_data_readiness
from app.services.risk_engine_service import get_latest_company_risk, recalculate_company_risk
from app.services.summary_engine_service import generate_company_summary, get_latest_company_summary
from app.contracts.orchestrator import CompanyCheckMode
from app.services.company_check_orchestrator import run_company_check


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

RISK_CLIENT_LABELS = {
    "CRITICAL": "Критический риск",
    "HIGH": "Высокий риск",
    "ATTENTION": "Требует внимания",
    "NO_MATERIAL_RISKS": "Существенных рисков не выявлено",
    "NO_MATERIAL_SIGNALS": "Существенных рисков не выявлено",
    "INSUFFICIENT_DATA": "Недостаточно данных",
    "CONFIRMED_RISK": "Подтверждённый риск",
    "WARNING": "Требует внимания",
    "INFO": "Информация",
    "NO_RISK_FOUND": "Проверено, сведений не найдено",
    "NOT_CHECKED": "Не проверено",
    "UNAVAILABLE": "Источник недоступен",
    "NOT_APPLICABLE": "Не применяется",
    "STALE": "Требуется обновление",
    "PARTIAL_COVERAGE": "Частичное покрытие",
    "DATA_QUALITY_REVIEW_REQUIRED": "Требуется проверка качества данных",
    "NONE": "Нет",
    "LOW": "Низкая",
    "MEDIUM": "Средняя",
}

RISK_SECTION_LABELS = {
    "registration": "Регистрационные сведения",
    "ownership_management": "Руководство и связи",
    "finance": "Финансы",
    "taxes": "Налоги",
    "enforcement": "Исполнительные производства",
    "bankruptcy": "Банкротство",
    "litigation": "Суды",
    "procurement": "Закупки",
    "licences_regulatory": "Лицензии и допуски",
    "compliance": "Регуляторные проверки",
}

SOURCE_CLIENT_LABELS = {
    "dadata": "DaData",
    "excel_import": "База сервиса",
    "fns": "ФНС — реестровые данные",
    "fns_headcount": "ФНС — численность",
    "fns_msp": "ФНС — реестр МСП",
    "fns_snr": "ФНС — налоговые режимы организаций",
    "fns_snrip": "ФНС — налоговые режимы предпринимателей",
    "fns_revenue_expenses": "ФНС — доходы и расходы",
    "fns_tax_debt": "ФНС — налоговая задолженность",
    "fns_tax_offence": "ФНС — налоговые правонарушения",
    "fns_tax_paid": "ФНС — уплаченные налоги",
    "fns_disqualified": "ФНС — дисквалифицированные лица",
    "fns_sme_support": "ФНС — поддержка малого и среднего бизнеса",
    "fns_bankinform": "ФНС — приостановления операций по счетам",
    "girbo": "ГИР БО",
    "erknm_inspections": "Единый реестр контрольных мероприятий",
    "cbr_warning_list": "Банк России — предупредительный список",
    "cbr_zsk": "Банк России — платформа ЗСК",
    "roszdravnadzor": "Росздравнадзор",
    "roskomnadzor": "Роскомнадзор",
    "prime_disclosure": "ПРАЙМ — раскрытие информации",
    "moscow_general_court_cases": "Официальные суды Москвы",
    "checko_arbitration_cases": "Арбитражные дела",
    "fssp": "ФССП России",
}


def risk_label(value):
    return RISK_CLIENT_LABELS.get(str(value), str(value))


def risk_section_label(value):
    return RISK_SECTION_LABELS.get(str(value), str(value))


def source_label(value):
    return SOURCE_CLIENT_LABELS.get(str(value), "Официальный или публичный источник")


def format_date_ru(value):
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y")
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    if isinstance(value, int) and 1000 <= value <= 9999:
        return str(value)
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%d.%m.%Y")
    except ValueError:
        return text


templates.env.filters["risk_label"] = risk_label
templates.env.filters["risk_section_label"] = risk_section_label
templates.env.filters["date_ru"] = format_date_ru
templates.env.filters["source_label"] = source_label


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


def validate_company_inn(
    value: str,
):
    clean_inn = normalize_inn(value)

    if len(clean_inn) not in (10, 12):
        raise HTTPException(
            status_code=400,
            detail=(
                "ИНН должен содержать "
                "10 или 12 цифр"
            ),
        )

    return clean_inn


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

    if is_full_inn(query):
        inn = normalize_inn(query)

        return RedirectResponse(
            url=f"/company/{inn}",
            status_code=302,
        )

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
    clean_inn = validate_company_inn(inn)

    company = get_company_for_web(
        clean_inn
    )

    if company is None:
        raise HTTPException(
            status_code=404,
            detail="Компания не найдена",
        )

    risk_assessment = get_latest_company_risk(clean_inn)
    company["risk_assessment"] = (
        risk_assessment.model_dump(mode="json") if risk_assessment else None
    )
    summary = get_latest_company_summary(clean_inn)
    company["summary"] = summary.model_dump(mode="json") if summary else None

    company = prepare_company_for_template(
        company
    )

    return templates.TemplateResponse(
        request=request,
        name="company.html",
        context={
            "company": company,
        },
    )


@app.post("/company/{inn}/risk-assessment")
async def company_risk_assessment(inn: str):
    """Explicit on-demand calculation; opening a card never triggers it."""
    clean_inn = validate_company_inn(inn)
    try:
        recalculate_company_risk(clean_inn)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return RedirectResponse(url=f"/company/{clean_inn}", status_code=303)


@app.post("/company/{inn}/summary")
async def company_summary(inn: str):
    """Explicit deterministic summary generation from the latest saved risk assessment."""
    clean_inn = validate_company_inn(inn)
    try:
        generate_company_summary(clean_inn)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return RedirectResponse(url=f"/company/{clean_inn}", status_code=303)


@app.post("/company/{inn}/check")
async def company_full_check(inn: str):
    """Run the bounded FULL orchestration flow for one company."""
    clean_inn = validate_company_inn(inn)
    try:
        run_company_check(clean_inn, mode=CompanyCheckMode.FULL)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return RedirectResponse(url=f"/company/{clean_inn}", status_code=303)


@app.post(
    "/company/{inn}/npd-check",
)
async def company_npd_check(
    inn: str,
):
    clean_inn = validate_company_inn(inn)

    refresh_npd_check_for_inn(
        clean_inn
    )

    return RedirectResponse(
        url=f"/company/{clean_inn}",
        status_code=303,
    )


@app.post(
    "/company/{inn}/cbr-finorg-check",
)
async def company_cbr_finorg_check(
    inn: str,
):
    clean_inn = validate_company_inn(inn)

    refresh_cbr_finorg_check_for_inn(
        clean_inn
    )

    return RedirectResponse(
        url=f"/company/{clean_inn}",
        status_code=303,
    )


@app.post("/company/{inn}/roszdrav-license-check")
async def company_roszdrav_license_check(inn: str):
    clean_inn = validate_company_inn(inn)
    refresh_roszdrav_unified_license_check(clean_inn)
    return RedirectResponse(url=f"/company/{clean_inn}", status_code=303)


@app.post("/company/{inn}/roskomnadzor-pd-check")
async def company_roskomnadzor_pd_check(inn: str):
    clean_inn = validate_company_inn(inn)
    refresh_pd_operator_check(clean_inn)
    return RedirectResponse(url=f"/company/{clean_inn}", status_code=303)


@app.post("/company/{inn}/sro-check")
async def company_sro_check(inn: str):
    clean_inn = validate_company_inn(inn)
    refresh_nostroy_check(clean_inn, applicable=True)
    refresh_nopriz_check(clean_inn, applicable=True)
    return RedirectResponse(url=f"/company/{clean_inn}", status_code=303)


@app.post("/company/{inn}/corporate-disclosure-check")
async def company_corporate_disclosure_check(inn: str):
    clean_inn = validate_company_inn(inn)
    refresh_corporate_disclosure_check(clean_inn)
    return RedirectResponse(url=f"/company/{clean_inn}", status_code=303)


@app.post("/company/{inn}/arbitration-courts-check")
async def company_arbitration_courts_check(inn: str, deepen: bool = False):
    clean_inn = validate_company_inn(inn)
    refresh_arbitration_court_check(clean_inn, deepen=deepen)
    return RedirectResponse(url=f"/company/{clean_inn}", status_code=303)


@app.post("/company/{inn}/general-courts-check")
async def company_general_courts_check(inn: str):
    clean_inn = validate_company_inn(inn)
    refresh_general_court_check(clean_inn)
    return RedirectResponse(url=f"/company/{clean_inn}", status_code=303)


@app.post("/company/{inn}/protected-source/{source_code}/start")
async def company_protected_source_start(inn: str, source_code: str):
    clean_inn = validate_company_inn(inn)
    start_protected_source_session(clean_inn, source_code)
    return RedirectResponse(url=f"/company/{clean_inn}", status_code=303)


# =========================================================
# API: COMPANY
# =========================================================


@app.get(
    "/api/company/{inn}",
)
async def api_company(
    inn: str,
):
    clean_inn = validate_company_inn(inn)

    company = get_company_for_web(
        clean_inn
    )

    if company is None:
        raise HTTPException(
            status_code=404,
            detail="Компания не найдена",
        )

    return company


@app.post(
    "/api/company/{inn}/npd-check",
)
async def api_company_npd_check(
    inn: str,
):
    clean_inn = validate_company_inn(inn)

    return refresh_npd_check_for_inn(
        clean_inn
    )


@app.post(
    "/api/company/{inn}/cbr-finorg-check",
)
async def api_company_cbr_finorg_check(
    inn: str,
):
    clean_inn = validate_company_inn(inn)

    return refresh_cbr_finorg_check_for_inn(
        clean_inn
    )


@app.post("/api/company/{inn}/roszdrav-license-check")
async def api_company_roszdrav_license_check(inn: str):
    return refresh_roszdrav_unified_license_check(validate_company_inn(inn))


@app.post("/api/company/{inn}/roskomnadzor-pd-check")
async def api_company_roskomnadzor_pd_check(inn: str):
    return refresh_pd_operator_check(validate_company_inn(inn))


@app.post("/api/company/{inn}/sro-check")
async def api_company_sro_check(inn: str):
    clean_inn = validate_company_inn(inn)
    return {
        "nostroy": refresh_nostroy_check(clean_inn, applicable=True),
        "nopriz": refresh_nopriz_check(clean_inn, applicable=True),
    }


@app.post("/api/company/{inn}/corporate-disclosure-check")
async def api_company_corporate_disclosure_check(inn: str):
    return refresh_corporate_disclosure_check(validate_company_inn(inn))


@app.post("/api/company/{inn}/arbitration-courts-check")
async def api_company_arbitration_courts_check(inn: str, deepen: bool = False):
    return refresh_arbitration_court_check(validate_company_inn(inn), deepen=deepen)


@app.post("/api/company/{inn}/general-courts-check")
async def api_company_general_courts_check(inn: str):
    return refresh_general_court_check(validate_company_inn(inn))


@app.post("/api/company/{inn}/check")
async def api_company_check(
    inn: str,
    mode: CompanyCheckMode = Query(default=CompanyCheckMode.QUICK),
):
    return run_company_check(validate_company_inn(inn), mode=mode)


@app.post("/api/company/{inn}/protected-source/{source_code}/start")
async def api_company_protected_source_start(inn: str, source_code: str):
    return start_protected_source_session(validate_company_inn(inn), source_code)


@app.post("/api/roszdrav/medical-device-check")
async def api_roszdrav_medical_device_check(
    registration_number: str = Query(min_length=1, max_length=160),
):
    return refresh_roszdrav_medical_device_check(registration_number)


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
        "count": len(results),
        "results": results,
    }


# =========================================================
# HEALTH
# =========================================================


@app.get("/internal/data-readiness", response_class=HTMLResponse, include_in_schema=False)
async def internal_data_readiness_page(request: Request):
    response = templates.TemplateResponse(
        request=request,
        name="data_readiness.html",
        context={"readiness": get_data_readiness()},
    )
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/internal/api/data-readiness", include_in_schema=False)
async def internal_data_readiness_api():
    return get_data_readiness()


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

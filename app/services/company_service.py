import re
from difflib import SequenceMatcher

from sqlalchemy import case, desc, func, literal, or_, select
from sqlalchemy.orm import selectinload

from app.database.postgres import get_session
from app.models.company import (
    Company,
    CompanyManager,
)
from app.providers.dadata_provider import DadataCompanyProvider


provider = DadataCompanyProvider()


def company_to_dict(company: Company):
    manager = None

    for item in company.managers:
        if item.is_current:
            manager = item
            break

    if manager is None and company.managers:
        manager = company.managers[0]

    financial = None

    if company.financials:
        financial = company.financials[0]

    phones = []
    emails = []
    websites = []

    for contact in company.contacts:
        if contact.contact_type == "phone":
            phones.append(contact.value)

        elif contact.contact_type == "email":
            emails.append(contact.value)

        elif contact.contact_type == "website":
            websites.append(contact.value)

    branches = []

    for branch in company.branches:
        branches.append(
            {
                "name": branch.name,
                "address": branch.address,
                "kpp": branch.kpp,
                "branch_code": branch.branch_code,
            }
        )

    return {
        "id": company.id,
        "inn": company.inn,
        "kpp": company.kpp,
        "ogrn": company.ogrn,
        "okpo": company.okpo,
        "name": company.name,
        "short_name": company.short_name,
        "full_name": company.full_name,
        "address": company.address,
        "activity": company.activity,
        "okved": company.okved,
        "website": company.website,
        "status": company.status,

        "director_name": (
            manager.full_name
            if manager
            else None
        ),

        "director_position": (
            manager.position
            if manager
            else None
        ),

        "revenue": (
            financial.revenue
            if financial
            else None
        ),

        "company_value": (
            financial.company_value
            if financial
            else None
        ),

        "employee_count": (
            financial.employee_count
            if financial
            else None
        ),

        "phones": phones,
        "emails": emails,
        "websites": websites,
        "branches": branches,

        "registration_date": None,
        "risk_score": 0,

        "source": company.source or "database",
    }


def get_company_from_database(inn: str):
    session = get_session()

    try:
        statement = (
            select(Company)
            .where(
                Company.inn == inn
            )
            .options(
                selectinload(
                    Company.managers
                ),
                selectinload(
                    Company.contacts
                ),
                selectinload(
                    Company.financials
                ),
                selectinload(
                    Company.identifiers
                ),
                selectinload(
                    Company.branches
                ),
            )
        )

        company = session.execute(
            statement
        ).scalar_one_or_none()

        if company is None:
            return None

        return company_to_dict(
            company
        )

    finally:
        session.close()


def save_provider_company(data):
    session = get_session()

    try:
        existing_company = session.execute(
            select(Company).where(
                Company.inn == data["inn"]
            )
        ).scalar_one_or_none()

        if existing_company is not None:
            return existing_company.id

        company = Company(
            inn=data.get("inn"),
            kpp=data.get("kpp"),
            ogrn=data.get("ogrn"),

            name=(
                data.get("name")
                or data.get("short_name")
                or data.get("full_name")
                or data.get("inn")
            ),

            short_name=data.get(
                "short_name"
            ),

            full_name=data.get(
                "full_name"
            ),

            address=data.get(
                "address"
            ),

            okved=data.get(
                "okved"
            ),

            status=data.get(
                "status"
            ),

            source="dadata",
        )

        session.add(
            company
        )

        session.flush()

        director_name = data.get(
            "director_name"
        )

        if director_name:
            manager = CompanyManager(
                company_id=company.id,

                full_name=director_name,

                position=data.get(
                    "director_position"
                ),

                is_current=True,

                source="dadata",
            )

            session.add(
                manager
            )

        session.commit()

        return company.id

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


def get_company_by_inn(inn: str):
    company = get_company_from_database(
        inn
    )

    if company is not None:
        company["source"] = "database"
        return company

    external_company = provider.get_company(
        inn
    )

    if external_company is None:
        return None

    save_provider_company(
        external_company
    )

    company = get_company_from_database(
        inn
    )

    if company is not None:
        company["source"] = "dadata"

    return company


LEGAL_FORM_PATTERN = re.compile(
    r"\b(?:общество\s+с\s+ограниченной\s+ответственностью|публичное\s+акционерное\s+общество|"
    r"акционерное\s+общество|закрытое\s+акционерное\s+общество|индивидуальный\s+предприниматель|"
    r"ооо|пао|оао|ао|зао|ип)\b",
    re.IGNORECASE,
)


def normalize_company_name(value: str) -> str:
    value = str(value or "").casefold().replace("ё", "е")
    value = re.sub(r"[«»„“”\"'`()\[\]{},.]", " ", value)
    value = LEGAL_FORM_PATTERN.sub(" ", value)
    return " ".join(value.split())


def _search_result(company: Company) -> dict:
    return {
        "inn": company.inn,
        "kpp": company.kpp,
        "ogrn": company.ogrn,
        "name": company.name,
        "address": company.address,
        "activity": company.activity,
    }


def search_companies(query: str, limit: int = 20):
    query = " ".join(query.strip().split())

    if not query:
        return []

    session = get_session()

    try:
        if query.isdigit():
            # Identifier matches are deterministic and always outrank prefixes.
            statement = (
                select(Company)
                .where(
                    or_(
                        Company.inn == query,
                        Company.ogrn == query,
                        Company.inn.startswith(query),
                    )
                )
                .order_by(
                    case(
                        (Company.inn == query, 0),
                        (Company.ogrn == query, 1),
                        else_=2,
                    ),
                    Company.name,
                )
                .limit(limit)
            )

        else:
            raw = query.casefold().replace("ё", "е")
            normalized = normalize_company_name(query)
            lowered_name = func.lower(Company.name)
            similarity = func.greatest(
                func.word_similarity(raw, lowered_name),
                func.word_similarity(normalized, lowered_name),
            )
            # Candidate lookup contains only expressions supported by the
            # lower(name) pg_trgm index.  Legal-form normalization and final
            # ranking run in Python over this bounded candidate set.
            statement = (
                select(Company)
                .where(
                    or_(
                        lowered_name == raw,
                        lowered_name.startswith(raw),
                        lowered_name.ilike(f"%{raw}%"),
                        lowered_name.ilike(f"%{normalized}%"),
                        literal(raw).op("<%")(lowered_name),
                        literal(normalized).op("<%")(lowered_name),
                    )
                )
                .order_by(desc(similarity), Company.name)
                .limit(max(100, limit * 10))
            )

        companies = (
            session.execute(
                statement
            )
            .scalars()
            .all()
        )

        if query.isdigit():
            return [_search_result(company) for company in companies]

        def rank(company: Company):
            name = company.name.casefold().replace("ё", "е")
            canonical = normalize_company_name(company.name)
            tier = (
                0 if name == raw
                else 1 if canonical == normalized
                else 2 if name.startswith(raw) or canonical.startswith(normalized)
                else 3 if raw in name or normalized in canonical
                else 4
            )
            fuzzy = max(
                SequenceMatcher(None, name, raw).ratio(),
                SequenceMatcher(None, canonical, normalized).ratio(),
            )
            return tier, -fuzzy, company.name

        companies.sort(key=rank)
        return [_search_result(company) for company in companies[:limit]]

    finally:
        session.close()

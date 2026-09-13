from sqlalchemy import select
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


def search_companies(query: str, limit: int = 20):
    query = query.strip()

    if not query:
        return []

    session = get_session()

    try:
        if query.isdigit():

            statement = (
                select(Company)
                .where(
                    Company.inn.startswith(query)
                )
                .order_by(
                    Company.name
                )
                .limit(limit)
            )

        else:

            statement = (
                select(Company)
                .where(
                    Company.name.ilike(
                        f"%{query}%"
                    )
                )
                .order_by(
                    Company.name
                )
                .limit(limit)
            )

        companies = (
            session.execute(
                statement
            )
            .scalars()
            .all()
        )

        results = []

        for company in companies:
            results.append(
                {
                    "id": company.id,
                    "inn": company.inn,
                    "kpp": company.kpp,
                    "ogrn": company.ogrn,
                    "name": company.name,
                    "address": company.address,
                    "activity": company.activity,
                }
            )

        return results

    finally:
        session.close()
from datetime import date, datetime, timezone

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.aggregators import company_aggregator
from app.database.postgres import engine
from app.models.company import Company
from app.models.headcount import CompanyHeadcount
from app.models.msp import CompanyMspProfile
from app.models.source import DataSet, DataSource
from app.models.tax_regime import CompanyTaxRegimeSnapshot
from app.services import headcount_service, msp_service, tax_regime_service


BOUNDARY = datetime(2026, 9, 25, 23, 59, tzinfo=timezone.utc)
NEXT_DAY = datetime(2026, 9, 26, tzinfo=timezone.utc)


@pytest.fixture
def product_db(monkeypatch):
    connection = engine.connect()
    transaction = connection.begin()
    assert connection.scalar(sa.text("SELECT current_database()")) != "kontragent"
    factory = sessionmaker(
        bind=connection,
        autoflush=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    monkeypatch.setattr(headcount_service, "get_session", factory)
    monkeypatch.setattr(msp_service, "get_session", factory)
    monkeypatch.setattr(tax_regime_service, "get_session", factory)
    try:
        with factory() as session:
            source = DataSource(
                code="fns_product_freshness_test",
                name="FNS product freshness test",
                source_type="official",
                priority=10,
                enabled=True,
            )
            session.add(source)
            session.flush()

            datasets = {}
            for code in (
                "fns_headcount",
                "fns_msp",
                "fns_tax_regime",
                "fns_snr",
                "fns_snrip",
            ):
                dataset = session.scalar(sa.select(DataSet).where(DataSet.code == code))
                if dataset is None:
                    dataset = DataSet(
                        source_id=source.id,
                        code=code,
                        name=code,
                        domain=code,
                        update_mode="bulk",
                        data_format="xml",
                        priority=10,
                    )
                    session.add(dataset)
                    session.flush()
                dataset.operational_status = "current"
                dataset.official_actual_until = BOUNDARY.date()
                dataset.last_data_date = date(2026, 9, 10)
                dataset.source_as_of = datetime(2026, 9, 10, tzinfo=timezone.utc)
                dataset.last_success_at = datetime(2026, 9, 25, tzinfo=timezone.utc)
                datasets[code] = dataset

            companies = {
                "legal_found": Company(
                    inn="7700000001",
                    ogrn="1027700000001",
                    entity_type="legal",
                    name="Legal found",
                ),
                "legal_missing": Company(
                    inn="7700000002",
                    ogrn="1027700000002",
                    entity_type="legal",
                    name="Legal missing",
                ),
                "ip_found": Company(
                    inn="770000000001",
                    ogrn="304770000000001",
                    entity_type="individual_entrepreneur",
                    name="IP found",
                ),
                "ip_missing": Company(
                    inn="770000000002",
                    ogrn="304770000000002",
                    entity_type="individual_entrepreneur",
                    name="IP missing",
                ),
            }
            session.add_all(companies.values())
            session.flush()
            session.add(
                CompanyHeadcount(
                    company_id=companies["legal_found"].id,
                    dataset_id=datasets["fns_headcount"].id,
                    year=2025,
                    employee_count=42,
                )
            )
            session.add_all(
                [
                    CompanyMspProfile(
                        company_id=companies[name].id,
                        dataset_id=datasets["fns_msp"].id,
                        data_date=date(2026, 9, 10),
                        category_code="1",
                        employee_count=7,
                    )
                    for name in ("legal_found", "ip_found")
                ]
            )
            session.add_all(
                [
                    CompanyTaxRegimeSnapshot(
                        company_id=companies["legal_found"].id,
                        dataset_id=datasets["fns_snr"].id,
                        entity_type="legal",
                        data_date=date(2026, 9, 25),
                        regime_codes=["usn"],
                    ),
                    CompanyTaxRegimeSnapshot(
                        company_id=companies["ip_found"].id,
                        dataset_id=datasets["fns_snrip"].id,
                        entity_type="individual_entrepreneur",
                        data_date=date(2026, 9, 25),
                        regime_codes=["psn"],
                    ),
                ]
            )
            session.commit()
            ids = {name: company.id for name, company in companies.items()}
        yield factory, ids
    finally:
        transaction.rollback()
        connection.close()


def test_postgresql_actual_until_boundary_then_next_day_hides_positive_facts(product_db):
    _factory, ids = product_db

    assert headcount_service.get_headcount_check_for_company(
        ids["legal_found"], now=BOUNDARY
    )["result"] == "found"
    assert msp_service.get_msp_check_for_company(
        ids["legal_found"], now=BOUNDARY
    )["result"] == "found"
    assert tax_regime_service.get_tax_regime_check_for_company(
        ids["legal_found"], now=BOUNDARY
    )["result"] == "found"
    assert tax_regime_service.get_tax_regime_check_for_company(
        ids["ip_found"], now=BOUNDARY
    )["member_dataset_code"] == "fns_snrip"

    for check in (
        headcount_service.get_headcount_check_for_company(
            ids["legal_found"], now=NEXT_DAY
        ),
        msp_service.get_msp_check_for_company(ids["legal_found"], now=NEXT_DAY),
        tax_regime_service.get_tax_regime_check_for_company(
            ids["legal_found"], now=NEXT_DAY
        ),
        tax_regime_service.get_tax_regime_check_for_company(
            ids["ip_found"], now=NEXT_DAY
        ),
    ):
        assert check["result"] == "unavailable"
        assert check["checked"] is False
        assert check["reason"] == "dataset_stale"

    assert headcount_service.get_latest_headcount_for_company(
        ids["legal_found"], now=NEXT_DAY
    ) is None
    assert msp_service.get_msp_profile_for_company(
        ids["legal_found"], now=NEXT_DAY
    ) is None
    assert tax_regime_service.get_tax_regime_profile_for_company(
        ids["legal_found"], now=NEXT_DAY
    ) is None


def test_postgresql_fresh_absence_is_not_found_and_applicability_is_explicit(product_db):
    _factory, ids = product_db

    for check in (
        headcount_service.get_headcount_check_for_company(
            ids["legal_missing"], now=BOUNDARY
        ),
        msp_service.get_msp_check_for_company(ids["legal_missing"], now=BOUNDARY),
        msp_service.get_msp_check_for_company(ids["ip_missing"], now=BOUNDARY),
        tax_regime_service.get_tax_regime_check_for_company(
            ids["legal_missing"], now=BOUNDARY
        ),
        tax_regime_service.get_tax_regime_check_for_company(
            ids["ip_missing"], now=BOUNDARY
        ),
    ):
        assert check["result"] == "not_found"
        assert check["checked"] is True
        assert check["source_data_date"] == date(2026, 9, 10)
        assert check["limitation"]

    headcount_ip = headcount_service.get_headcount_check_for_company(
        ids["ip_found"], now=BOUNDARY
    )
    assert headcount_ip["result"] == "not_applicable"
    assert headcount_ip["applicable"] is False


def test_postgresql_error_status_hides_existing_msp_fact(product_db):
    factory, ids = product_db
    with factory() as session:
        dataset = session.scalar(sa.select(DataSet).where(DataSet.code == "fns_msp"))
        dataset.operational_status = "error"
        session.commit()

    check = msp_service.get_msp_check_for_company(ids["legal_found"], now=BOUNDARY)
    assert check["result"] == "unavailable"
    assert check["reason"] == "dataset_error"
    assert msp_service.get_msp_profile_for_company(
        ids["legal_found"], now=BOUNDARY
    ) is None


def _product_payload(mode: str):
    base = {
        "id": 1,
        "inn": "7700000001",
        "name": "Product test",
        "entity_type": "legal",
        "sources_used": ["excel_import"],
        "employee_count": None,
        "employee_count_year": None,
        "msp_profile": None,
        "tax_regime_profile": None,
    }
    if mode == "found":
        base.update(
            {
                "employee_count": 42,
                "employee_count_year": 2025,
                "headcount_check": {"result": "found"},
                "msp_check": {"result": "found"},
                "msp_profile": {
                    "category_name": "Микропредприятие",
                    "inclusion_date": None,
                    "data_date": "2026-09-10",
                    "employee_count": 7,
                },
                "tax_regime_check": {"result": "found"},
                "tax_regime_profile": {
                    "regimes": [{"code": "usn", "name": "УСН"}],
                    "data_date": "2026-09-25",
                },
            }
        )
    elif mode == "not_found":
        base.update(
            {
                "headcount_check": {"result": "not_found"},
                "msp_check": {"result": "not_found"},
                "tax_regime_check": {"result": "not_found"},
            }
        )
    else:
        base.update(
            {
                "headcount_check": {"result": "unavailable", "reason": "dataset_stale"},
                "msp_check": {"result": "unavailable", "reason": "dataset_stale"},
                "tax_regime_check": {
                    "result": "unavailable",
                    "reason": "dataset_stale",
                },
            }
        )
    return base


@pytest.mark.parametrize("mode", ("found", "not_found", "unavailable"))
def test_api_and_card_expose_distinct_product_states(monkeypatch, mode):
    import main

    monkeypatch.setattr(main, "get_company_for_web", lambda _inn: _product_payload(mode))
    monkeypatch.setattr(main, "get_latest_company_risk", lambda _inn: None)
    monkeypatch.setattr(main, "get_latest_company_summary", lambda _inn: None)
    with TestClient(main.app) as client:
        api = client.get("/api/company/7700000001")
    company = _product_payload(mode)
    card_text = "".join(
        main.templates.env.get_template(template).render(company=company)
        for template in (
            "partials/financials.html",
            "partials/msp.html",
            "partials/tax_regime.html",
        )
    )

    assert api.status_code == 200
    payload = api.json()
    assert payload["headcount_check"]["result"] == mode
    assert payload["msp_check"]["result"] == mode
    assert payload["tax_regime_check"]["result"] == mode
    if mode == "found":
        assert "42" in card_text
        assert "Микропредприятие" in card_text
        assert "УСН" in card_text
    elif mode == "not_found":
        assert "ИНН не найден в актуальном наборе ФНС" in card_text
        assert "ИНН не найден в актуальном официальном реестре МСП" in card_text
    else:
        assert "Проверка численности временно недоступна" in card_text
        assert "Проверка реестра МСП временно недоступна" in card_text
        assert payload["sources_used"] == ["excel_import"]


def test_api_and_card_expose_ip_applicability(monkeypatch):
    import main

    company = {
        **_product_payload("found"),
        "inn": "770000000001",
        "entity_type": "individual_entrepreneur",
        "employee_count": None,
        "employee_count_year": None,
        "headcount_check": {
            "result": "not_applicable",
            "reason": "legal_entities_only",
        },
        "tax_regime_check": {
            "result": "found",
            "member_dataset_code": "fns_snrip",
        },
        "tax_regime_profile": {
            "regimes": [{"code": "psn", "name": "ПСН"}],
            "data_date": "2026-09-25",
        },
    }
    monkeypatch.setattr(main, "get_company_for_web", lambda _inn: company)
    monkeypatch.setattr(main, "get_latest_company_risk", lambda _inn: None)
    monkeypatch.setattr(main, "get_latest_company_summary", lambda _inn: None)

    with TestClient(main.app) as client:
        response = client.get("/api/company/770000000001")
    card_text = "".join(
        main.templates.env.get_template(template).render(company=company)
        for template in (
            "partials/financials.html",
            "partials/msp.html",
            "partials/tax_regime.html",
        )
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["headcount_check"]["result"] == "not_applicable"
    assert payload["msp_check"]["result"] == "found"
    assert payload["tax_regime_check"]["member_dataset_code"] == "fns_snrip"
    assert "Не применяется к ИП" in card_text
    assert "Микропредприятие" in card_text
    assert "ПСН" in card_text


def test_unavailable_checks_are_not_added_to_sources_used(monkeypatch):
    monkeypatch.setattr(company_aggregator, "get_source_registry", lambda: {})
    monkeypatch.setattr(
        company_aggregator,
        "get_company_from_database",
        lambda inn: {"id": 1, "inn": inn, "name": "test", "source": "excel_import"},
    )
    monkeypatch.setattr(
        company_aggregator,
        "normalize_base_company",
        lambda company: {"inn": company["inn"], "name": company["name"]},
    )
    monkeypatch.setattr(company_aggregator, "load_cached_source_candidates", lambda *_: [])
    monkeypatch.setattr(company_aggregator, "load_domain_candidates", lambda *_: [])
    monkeypatch.setattr(
        company_aggregator,
        "load_structured_domain_data",
        lambda _company_id: {
            "headcount_check": {"result": "unavailable"},
            "msp_profile": None,
            "msp_check": {"result": "unavailable"},
            "tax_regime_profile": None,
            "tax_regime_check": {"result": "unavailable"},
            "revenue_expense_check": None,
            "tax_debt": None,
            "tax_debt_check": None,
            "tax_debt_history": [],
            "tax_offence": None,
            "tax_offence_check": None,
            "tax_offence_history": [],
            "tax_payment": None,
            "tax_payment_check": None,
            "tax_payment_history": [],
            "legal_events": [],
        },
    )

    result = company_aggregator.aggregate_company("7700000001")
    assert result["sources_used"] == ["excel_import"]

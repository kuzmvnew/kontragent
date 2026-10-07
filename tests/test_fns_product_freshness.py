from datetime import date, datetime, timezone

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.aggregators import company_aggregator
from app.database.postgres import engine
from app.models.company import Company
from app.models.company_enrichment import CompanyEnrichmentRun, CompanySourceCoverage
from app.models.headcount import CompanyHeadcount
from app.models.msp import CompanyMspProfile
from app.models.source import DataSet, DataSource
from app.models.tax_regime import CompanyTaxRegimeSnapshot
from app.models.worker import WorkerPublicationState
from app.services import headcount_service, msp_service, tax_regime_service
from app.services.company_view_service import materialize_company_view_v1
from app.services.company_enrichment_service import _resolve_successful_coverage
from app.contracts.company_view_v1 import DataState
from uuid import uuid4


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
                if code in {"fns_tax_regime", "fns_snr", "fns_snrip"}:
                    dataset.enabled = True
                    dataset.source_url = (
                        "https://www.nalog.gov.ru/opendata/7707329152-snrip/"
                        if code == "fns_snrip"
                        else "https://www.nalog.gov.ru/opendata/7707329152-snr/"
                    )
                    dataset.coverage = {
                        "release_identity": f"{code}-release",
                        "artifact_sha256": "a" * 64,
                        "xsd_sha256": "b" * 64,
                    }
                datasets[code] = dataset
            datasets["fns_tax_regime"].coverage = {
                **datasets["fns_tax_regime"].coverage,
                "members": {
                    "legal": {"release_identity": "fns_snr-release"},
                    "ip": {"release_identity": "fns_snrip-release"},
                },
            }
            session.execute(
                sa.delete(WorkerPublicationState).where(
                    WorkerPublicationState.source_id == "fns_tax_regime"
                )
            )
            session.add(WorkerPublicationState(
                source_id="fns_tax_regime",
                generation=7,
                active_pointer="file:///accepted/c6-bundle.json",
                validation_metadata={
                    "checksum": "c" * 64,
                    "validation": {"release_identity": "fns_tax_regime-release"},
                },
            ))

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
                        data_date=date(2026, 9, 10),
                        regime_codes=["usn"],
                    ),
                    CompanyTaxRegimeSnapshot(
                        company_id=companies["ip_found"].id,
                        dataset_id=datasets["fns_snrip"].id,
                        entity_type="individual_entrepreneur",
                        data_date=date(2026, 9, 10),
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
    legal_check = tax_regime_service.get_tax_regime_check_for_company(
        ids["legal_found"], now=BOUNDARY
    )
    assert legal_check["provenance"] == {
        "source": "fns",
        "source_id": "fns_tax_regime",
        "member_dataset_code": "fns_snr",
        "official_source_url": "https://www.nalog.gov.ru/opendata/7707329152-snr/",
        "source_data_date": date(2026, 9, 10),
        "retrieved_at": None,
        "published_at": None,
        "family_release_identity": "fns_tax_regime-release",
        "member_release_identity": "fns_snr-release",
        "artifact_sha256": "a" * 64,
        "xsd_sha256": "b" * 64,
    }

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
        assert check["reason"].endswith("dataset_stale")

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
    ):
        assert check["result"] == "not_found"
        assert check["checked"] is True
        assert check["source_data_date"] == date(2026, 9, 10)
        assert check["limitation"]

    for name in ("legal_missing", "ip_missing"):
        check = tax_regime_service.get_tax_regime_check_for_company(
            ids[name], now=BOUNDARY
        )
        assert check["result"] == "unavailable"
        assert check["semantic_state"] == "NOT_CHECKED"
        assert check["reason"] == "company_not_checked_on_current_release"

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


def _tax_semantic_state(session, company_id):
    view = materialize_company_view_v1(
        session, company_id=company_id, generated_at=BOUNDARY
    )
    return next(
        fact.state
        for section in view.sections
        if section.section_key == "tax"
        for fact in section.facts
        if fact.anchor.field_key == "regime"
    )


@pytest.mark.parametrize(
    ("target", "sibling", "status", "error", "expected"),
    [
        ("legal_found", "fns_snrip", "unavailable", None, DataState.SOURCE_UNAVAILABLE),
        ("legal_found", "fns_snrip", "stale", None, DataState.STALE_DATA),
        ("legal_found", "fns_snrip", "error", "XSD schema mismatch", DataState.PARSING_ERROR),
        ("legal_found", "fns_snrip", "current", None, DataState.NOT_CHECKED),
        ("ip_found", "fns_snr", "unavailable", None, DataState.SOURCE_UNAVAILABLE),
        ("ip_found", "fns_snr", "stale", None, DataState.STALE_DATA),
        ("ip_found", "fns_snr", "error", "XML parse failed", DataState.PARSING_ERROR),
        ("ip_found", "fns_snr", "current", None, DataState.NOT_CHECKED),
    ],
)
def test_mandatory_sibling_failure_degrades_service_and_company_view(
    product_db, target, sibling, status, error, expected
):
    factory, ids = product_db
    with factory() as session:
        dataset = session.scalar(sa.select(DataSet).where(DataSet.code == sibling))
        dataset.operational_status = status
        dataset.last_error = error
        if expected == DataState.NOT_CHECKED:
            dataset.last_success_at = None
        session.flush()
        check = tax_regime_service.get_tax_regime_check_for_company(
            ids[target], now=BOUNDARY
        )
        assert check["result"] == "unavailable"
        assert check["semantic_state"] == expected.value
        assert _tax_semantic_state(session, ids[target]) == expected


def _add_tax_coverage(session, company_id, *, status, execution, generation=7):
    run = CompanyEnrichmentRun(
        company_id=company_id,
        trigger="c6-correction-test",
        idempotency_key=f"c6-{uuid4()}",
        applicable_sources=[],
        source_count=1,
    )
    session.add(run)
    session.flush()
    coverage = CompanySourceCoverage(
        enrichment_run_id=run.id,
        company_id=company_id,
        dataset_id=None,
        source_id="fns_tax_regime",
        worker_source_id="fns_tax_regime",
        mode="local_bulk_replay",
        status=status,
        execution_status=execution,
        source_snapshot={"release_identity": "fns_tax_regime-release"},
        handler_version="tax-regime-family-official-v1",
        publication_generation=generation,
        source_data_date=date(2026, 9, 10),
        replay_pointer="file:///accepted/c6-bundle.json",
        replay_checksum="c" * 64,
        fact_count=0,
        checked_at=BOUNDARY if execution == "succeeded" else None,
        updated_at=BOUNDARY,
    )
    session.add(coverage)
    session.flush()
    return coverage


def test_current_company_coverage_proves_negative_only_after_terminal_replay(product_db):
    factory, ids = product_db
    company_id = ids["legal_missing"]
    with factory() as session:
        assert _tax_semantic_state(session, company_id) == DataState.NOT_CHECKED
        initial = tax_regime_service.get_tax_regime_check_for_company(
            company_id, now=BOUNDARY
        )
        import main

        card_before = main.templates.env.get_template("partials/tax_regime.html").render(
            company={"tax_regime_check": initial, "tax_regime_profile": None}
        )
        assert "ещё не завершена" in card_before
        assert "ИНН не найден" not in card_before
        pending = _add_tax_coverage(
            session, company_id, status="PENDING", execution="pending"
        )
        assert tax_regime_service.get_tax_regime_check_for_company(
            company_id, now=BOUNDARY
        )["semantic_state"] == "NOT_CHECKED"
        assert _tax_semantic_state(session, company_id) == DataState.NOT_CHECKED

        pending.execution_status = "succeeded"
        _resolve_successful_coverage(session, pending, now=BOUNDARY)
        session.flush()
        checked = tax_regime_service.get_tax_regime_check_for_company(
            company_id, now=BOUNDARY
        )
        assert checked["result"] == "not_found"
        assert checked["provenance"]["family_release_identity"] == "fns_tax_regime-release"
        assert _tax_semantic_state(session, company_id) == DataState.NOT_FOUND
        card_after = main.templates.env.get_template("partials/tax_regime.html").render(
            company={"tax_regime_check": checked, "tax_regime_profile": None}
        )
        assert "ИНН не найден" in card_after

        pointer = session.get(WorkerPublicationState, "fns_tax_regime")
        pointer.generation = 8
        session.flush()
        assert tax_regime_service.get_tax_regime_check_for_company(
            company_id, now=BOUNDARY
        )["semantic_state"] == "NOT_CHECKED"
        assert _tax_semantic_state(session, company_id) == DataState.NOT_CHECKED


@pytest.mark.parametrize(
    ("status", "execution", "expected"),
    [
        ("PENDING", "queued", DataState.NOT_CHECKED),
        ("RUNNING", "running", DataState.NOT_CHECKED),
        ("SOURCE_UNAVAILABLE", "failed", DataState.SOURCE_UNAVAILABLE),
        ("TIMEOUT", "failed", DataState.TIMEOUT),
        ("PARSING_ERROR", "failed", DataState.PARSING_ERROR),
        ("STALE_DATA", "failed", DataState.STALE_DATA),
        ("ACCESS_REQUIRED", "blocked", DataState.SOURCE_UNAVAILABLE),
    ],
)
def test_nonterminal_or_failed_coverage_never_proves_negative(
    product_db, status, execution, expected
):
    factory, ids = product_db
    with factory() as session:
        _add_tax_coverage(
            session, ids["ip_missing"], status=status, execution=execution
        )
        check = tax_regime_service.get_tax_regime_check_for_company(
            ids["ip_missing"], now=BOUNDARY
        )
        assert check["result"] == "unavailable"
        assert check["semantic_state"] == expected.value
        assert _tax_semantic_state(session, ids["ip_missing"]) == expected


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

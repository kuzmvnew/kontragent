"""Opt-in real-artifact E2E for PUBLIC-CARD-BINDING-IMPL-01.

The test never calls Firmoteka.  It requires an isolated PostgreSQL database
preloaded from the retained real 0100000614 artifact and actual official rows.
"""

from datetime import UTC, datetime
import json
import os

import psycopg
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app.contracts.company_view_v1 import Audience, DataState, FinanceMetricCode
from app.database.postgres import engine
from app.models.semantic_fact import CompanySemanticFact
from app.services.company_view_service import build_company_view_v1, materialize_company_view_v1
from sqlalchemy.orm import Session
from public_app.contracts import PublicationInfo, SCHEMA_VERSION
from public_app.main import create_app
from public_app.semantic import validate_public_text
from scripts.export_public_release import build_projection


pytestmark = pytest.mark.skipif(
    os.getenv("PUBLIC_CARD_REAL_E2E") != "1",
    reason="requires isolated DB preloaded from retained real Firmoteka artifact",
)

INN = "0100000614"
NOW = datetime(2026, 9, 17, 22, tzinfo=UTC)
PINNED_REAL_EVIDENCE = {
    "company_id": 1,
    "raw_sha256": "33e528f8fd67cf6342539f6f49831451b3c62ee1f37eaed28463ef2f08a0e814",
    "snapshot_id": "1eac2a0e-c777-4173-bb71-6fd42cb0ca2d",
    "debt_periods": frozenset(
        {
            "DATE:2026-06-01",
            "DATE:2026-07-01",
            "DATE:2026-08-01",
            "DATE:2026-09-01",
        }
    ),
    "enforcement_source_data_date": "2026-09-27",
    "semantic_fact_count": 88,
}


@pytest.fixture(scope="module", autouse=True)
def _materialize_real_semantic_facts():
    with Session(engine) as session:
        company_id = session.scalar(
            sa.text("SELECT id FROM companies WHERE inn=:inn"), {"inn": INN}
        )
        assert company_id is not None
        materialize_company_view_v1(session, company_id=int(company_id), generated_at=NOW)
        session.commit()


def _projection():
    publication = PublicationInfo(
        schema_version=SCHEMA_VERSION,
        release_id="alan-real-e2e",
        published_at=NOW,
        result_date=NOW.date(),
        content_updated_at=NOW,
        index_eligible=False,
    )
    url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(url, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        with connection.cursor() as cursor:
            return build_projection(cursor, INN, publication)


def _view(audience: Audience):
    url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(url, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM companies WHERE inn=%s", (INN,))
            company_id = cursor.fetchone()["id"]
            return company_id, build_company_view_v1(
                cursor,
                company_id=company_id,
                audience=audience,
                generated_at=NOW,
            )


def _facts(view, section_key):
    section = next(item for item in view.sections if item.section_key == section_key)
    return section.facts


def _metric(view, metric, period):
    return next(
        item
        for item in view.finances
        if item.metric == metric and item.period.identity == period
    )


def _all_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _all_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _all_keys(child)


def _require_pinned_real_evidence():
    with engine.connect() as connection:
        row = connection.execute(
            sa.text(
                """SELECT c.id AS company_id, s.id AS snapshot_id,
                          s.raw_sha256,
                          s.projection->'enforcements'->>'snapshot'
                              AS enforcement_source_data_date
                   FROM companies c
                   JOIN firmoteka_company_snapshots s ON s.company_id=c.id
                   WHERE c.inn=:inn AND s.is_current=TRUE
                   ORDER BY s.retrieved_at DESC
                   LIMIT 1"""
            ),
            {"inn": INN},
        ).mappings().one_or_none()
    expected = PINNED_REAL_EVIDENCE
    if not row or (
        row["company_id"] != expected["company_id"]
        or str(row["snapshot_id"]) != expected["snapshot_id"]
        or row["raw_sha256"] != expected["raw_sha256"]
        or row["enforcement_source_data_date"]
        != expected["enforcement_source_data_date"]
    ):
        pytest.skip(
            "pinned retained Alan evidence is not installed in this disposable DB"
        )


def test_real_alan_firmoteka_to_semantic_official_risk_summary():
    _require_pinned_real_evidence()
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT current_database()")) != "kontragent"
    company_id, internal = _view(Audience.INTERNAL)
    resolved_id, authenticated = _view(Audience.AUTHENTICATED)
    public_id, public = _view(Audience.PUBLIC)
    assert resolved_id == public_id == company_id
    assert public.company_id is None
    assert authenticated.company_id == company_id
    assert internal.revision == authenticated.revision == public.revision
    legal_form = next(item for item in _facts(authenticated, "identity") if item.anchor.field_key == "legal_form")
    assert legal_form.selected_evidence.value == {
        "code": "12300",
        "name": "Общества с ограниченной ответственностью",
    }

    revenue = _metric(internal, FinanceMetricCode.REVENUE, "YEAR:2025")
    expenses = _metric(internal, FinanceMetricCode.EXPENSES, "YEAR:2025")
    profit_loss = _metric(internal, FinanceMetricCode.PROFIT_LOSS, "YEAR:2025")
    net_profit = _metric(internal, FinanceMetricCode.NET_PROFIT, "YEAR:2025")
    assert revenue.value == 9_673_000
    assert revenue.source == "REVEXP"
    revenue_fact = next(item for item in _facts(internal, "finances") if item.fact_ref == revenue.fact_ref)
    assert revenue_fact.state == DataState.FOUND
    assert [item.source_code for item in revenue_fact.alternative_evidence] == [
        "FIRMOTEKA_AUTHORIZED_BRIDGE"
    ]
    assert expenses.value == 9_638_000 and expenses.source == "REVEXP"
    assert profit_loss.value == 35_000 and profit_loss.source == "REVEXP"
    assert net_profit.value == 26_000 and net_profit.source == "FIRMOTEKA_AUTHORIZED_BRIDGE"
    assert profit_loss.metric != net_profit.metric
    assert revenue.source_data_date.isoformat() == "2025-12-31"

    founders = _facts(public, "founders")
    capital = _facts(public, "capital")
    assert len(founders) == 1
    assert founders[0].selected_evidence.value["share"] == "100%"
    assert capital[0].selected_evidence.value == {"amount": "10000", "currency": "RUB"}
    assert len([item for item in public.finances if item.metric == FinanceMetricCode.REVENUE]) == 4
    assert {
        item.period.identity: int(item.value)
        for item in public.finances
        if item.metric == FinanceMetricCode.EMPLOYEE_COUNT
    } == {"YEAR:2023": 16, "YEAR:2024": 12, "YEAR:2025": 6}
    assert {
        item.anchor.period_identity
        for item in _facts(public, "tax")
        if item.anchor.field_key == "debt"
    }
    assert len([item for item in _facts(public, "enforcement") if item.anchor.field_key == "case"]) == 13
    assert len(_facts(public, "events")) == 6
    enforcement = next(item for item in _facts(public, "enforcement") if item.anchor.field_key == "aggregate")
    assert (
        enforcement.selected_evidence.source_data_date.isoformat()
        == PINNED_REAL_EVIDENCE["enforcement_source_data_date"]
    )
    assert enforcement.selected_evidence.source_data_date.isoformat() != "2022-01-19"

    assert _metric(public, FinanceMetricCode.NET_PROFIT, "YEAR:2025").value == 26_000
    assert _metric(public, FinanceMetricCode.NET_PROFIT, "YEAR:2025").source == "FIRMOTEKA_AUTHORIZED_BRIDGE"
    assert _metric(public, FinanceMetricCode.REVENUE, "YEAR:2025").source == "REVEXP"
    public_revenue_fact = next(item for item in _facts(public, "finances") if item.fact_ref == revenue.fact_ref)
    assert [item.source_code for item in public_revenue_fact.alternative_evidence] == [
        "FIRMOTEKA_AUTHORIZED_BRIDGE"
    ]

    assert internal.risk_ref and internal.summary_ref
    with engine.connect() as connection:
        same_subject = connection.execute(
            sa.text(
                """SELECT r.company_id AS risk_company_id, s.company_id AS summary_company_id,
                          s.risk_assessment_id
                   FROM company_risk_assessments_v3 r
                   JOIN company_summaries_v3 s ON s.risk_assessment_id=r.assessment_id
                   WHERE r.assessment_id=:risk_ref AND s.summary_id=:summary_ref"""
            ),
            {"risk_ref": internal.risk_ref, "summary_ref": internal.summary_ref},
        ).mappings().one()
        assert same_subject["risk_company_id"] == same_subject["summary_company_id"] == company_id
        persisted_count = connection.scalar(
            sa.select(sa.func.count()).select_from(CompanySemanticFact).where(
                CompanySemanticFact.company_id == company_id,
                CompanySemanticFact.is_current.is_(True),
            )
        )
        assert persisted_count >= 80


def test_real_alan_pinned_debt_history_matches_retained_evidence_manifest():
    _require_pinned_real_evidence()
    company_id, public = _view(Audience.PUBLIC)
    assert company_id == PINNED_REAL_EVIDENCE["company_id"]
    periods = {
        item.anchor.period_identity
        for item in _facts(public, "tax")
        if item.anchor.field_key == "debt"
    }
    assert periods == PINNED_REAL_EVIDENCE["debt_periods"]
    projected = _projection()
    tax_section = projected.company_view.section("tax")
    assert {
        item.period
        for item in tax_section.facts("debt")
    } == PINNED_REAL_EVIDENCE["debt_periods"]
    with engine.connect() as connection:
        persisted_count = connection.scalar(
            sa.select(sa.func.count()).select_from(CompanySemanticFact).where(
                CompanySemanticFact.company_id == company_id,
                CompanySemanticFact.is_current.is_(True),
            )
        )
    assert persisted_count == PINNED_REAL_EVIDENCE["semantic_fact_count"]


def test_real_alan_public_api_ssr_revision_parity_and_no_leakage():
    projection = _projection()
    internal_meaning_ids = tuple(
        item.meaning_id for item in projection.risk.factors if item.meaning_id
    )

    class Repository:
        def get_company(self, inn):
            return projection if inn == INN else None

    client = TestClient(create_app(repository=Repository()))
    api = client.get(f"/api/company/{INN}")
    card = client.get(f"/companies/{INN}")
    assert api.status_code == card.status_code == 200
    payload = api.json()
    revision = payload["view"]["revision"]
    assert f'data-view-revision="{revision}"' in card.text
    assert payload["view"]["contract_version"] == "company-view-v1"
    assert len(payload["view"]["sections"]) == 21
    sections = {item["section_key"]: item for item in payload["view"]["sections"]}
    assert {
        "identity",
        "status",
        "registration",
        "address",
        "activity",
        "management",
        "founders",
        "capital",
        "finances",
        "employees",
        "tax",
        "enforcement",
        "events",
        "risk",
        "summary",
        "source_coverage",
        "freshness",
    }.issubset(sections)
    founder = sections["founders"]["items"]
    capital = sections["capital"]["items"]
    finances = sections["finances"]["items"]
    employees = sections["employees"]["items"]
    tax = sections["tax"]["items"]
    enforcement = sections["enforcement"]["items"]
    events = sections["events"]["items"]
    assert len(founder) == 1 and founder[0]["value"]["share"] == "100%"
    assert capital[0]["value"] == {"amount": "10000", "currency": "RUB"}
    assert len([item for item in finances if item["field_key"] == "REVENUE"]) == 4
    revenue_2025 = next(item for item in finances if item["field_key"] == "REVENUE" and item["period"] == "YEAR:2025")
    net_profit_2025 = next(item for item in finances if item["field_key"] == "NET_PROFIT" and item["period"] == "YEAR:2025")
    assert revenue_2025["value"]["value"] == "9673000.00"
    assert revenue_2025["source"]["name"] == "Доходы и расходы по данным ФНС"
    assert revenue_2025["alternative_sources"][0]["name"] == "Firmoteka · вторичный источник"
    assert net_profit_2025["value"]["value"] == "26000"
    assert net_profit_2025["source"]["name"] == "Firmoteka · вторичный источник"
    assert {item["period"]: item["value"] for item in employees} == {
        "YEAR:2023": 16,
        "YEAR:2024": 12,
        "YEAR:2025": 6,
    }
    assert [item for item in tax if item["field_key"] == "debt"]
    assert len([item for item in enforcement if item["field_key"] == "case"]) == 13
    assert len(events) == 6
    assert payload["assessment"]["title"]
    assert payload["summary"]["short_conclusion"]
    assert card.text.count('class="enforcement-case"') == 13
    assert card.text.count('class="company-event"') == 6
    assert card.text.count('class="semantic-card founder-card"') == 1
    assert "Чистая прибыль" in card.text and "26 000 ₽" in card.text
    assert "10 000 ₽" in card.text
    assert "2023" in card.text and ">16<" in card.text
    assert "Firmoteka · вторичный источник" in card.text
    assert "data-fact-ref=" in card.text and "data-item-ref=" in card.text
    forbidden = {
        "company_id",
        "raw_sha256",
        "parser_version",
        "worker_job_id",
        "worker_run_id",
        "raw_payload",
        "meaning_id",
        "page_sha256",
        "normalized_payload",
        "provider_row_id",
    }
    assert not (forbidden & set(_all_keys(payload)))
    rendered = api.text + card.text
    for value in (
        "FIRMOTEKA_AUTHORIZED_BRIDGE",
        "TAX_OFFENCE_PRESENT",
        "raw_sha256",
        "parser_version",
        "page_sha256",
        "provider_row_id",
        "010701178084",
    ):
        assert value not in rendered
    for value in internal_meaning_ids:
        assert value not in rendered
    assert "facts[]" not in rendered
    assert "normalized_payload" not in rendered
    assert "raw payload" not in rendered.casefold()
    assert "company_id" not in json.dumps(payload, ensure_ascii=False)
    validate_public_text(payload)

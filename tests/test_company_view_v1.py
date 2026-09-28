from datetime import UTC, date, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.contracts.company_view_v1 import (
    Audience,
    DataState,
    EvidenceSourceClass,
    FactRights,
    Freshness,
)
from app.database.postgres import engine
from app.models.company import Company
from app.models.semantic_fact import CompanySemanticFact
from app.services.company_view_service import (
    SemanticCandidate,
    filter_company_view,
    persist_semantic_facts,
    normalize_firmoteka_projection,
    resolve_company_by_inn,
    select_semantic_facts,
)


NOW = datetime(2026, 9, 17, 20, tzinfo=UTC)


def _candidate(
    *,
    company_id: int = 1,
    value="value",
    source="BRIDGE",
    source_class=EvidenceSourceClass.AUTHORIZED_BRIDGE,
    rights=FactRights.AUTHENTICATED_ONLY,
    source_date=date(2025, 12, 31),
):
    return SemanticCandidate(
        company_id=company_id,
        section_key="finances",
        field_key="REVENUE",
        period_identity="YEAR:2025",
        value=value,
        source_code=source,
        source_class=source_class,
        evidence_identity=f"{source}:2025",
        source_data_date=source_date,
        retrieved_at=NOW,
        confidence=1,
        freshness=Freshness.CURRENT,
        rights=rights,
    )


def test_stable_refs_do_not_depend_on_value_source_or_order():
    first = select_semantic_facts((_candidate(value="1"),))[0]
    changed = select_semantic_facts(
        (
            _candidate(
                value="2",
                source="OFFICIAL",
                source_class=EvidenceSourceClass.OFFICIAL_API_OPEN_DATA,
                rights=FactRights.PUBLIC,
            ),
            _candidate(value="1"),
        )
    )[0]
    reversed_order = select_semantic_facts(tuple(reversed((_candidate(value="1"), _candidate(value="2")))))[0]
    assert first.fact_ref == changed.fact_ref == reversed_order.fact_ref
    assert first.item_ref == changed.item_ref == reversed_order.item_ref


def test_precedence_official_beats_bridge_and_preserves_alternative():
    fact = select_semantic_facts(
        (
            _candidate(value={"value": "9673000"}),
            _candidate(
                value={"value": "9673000.00"},
                source="REVEXP",
                source_class=EvidenceSourceClass.OFFICIAL_API_OPEN_DATA,
                rights=FactRights.PUBLIC,
            ),
        )
    )[0]
    assert fact.selected_evidence.source_code == "REVEXP"
    assert fact.alternative_evidence[0].source_code == "BRIDGE"
    assert fact.state == DataState.FOUND


def test_bridge_fallback_and_material_conflict():
    fallback = select_semantic_facts((_candidate(value="10"),))[0]
    assert fallback.selected_evidence.source_class == EvidenceSourceClass.AUTHORIZED_BRIDGE
    assert fallback.rights == FactRights.AUTHENTICATED_ONLY
    conflict = select_semantic_facts(
        (
            _candidate(value="10"),
            _candidate(
                value="11",
                source="OFFICIAL",
                source_class=EvidenceSourceClass.OFFICIAL_PRIMARY,
                rights=FactRights.PUBLIC,
            ),
        )
    )[0]
    assert conflict.state == DataState.CONFLICTING_EVIDENCE


def test_tie_break_uses_deterministic_source_code():
    a = _candidate(value="same", source="A")
    b = _candidate(value="same", source="B")
    first = select_semantic_facts((b, a))[0]
    second = select_semantic_facts((a, b))[0]
    assert first.selected_evidence.source_code == second.selected_evidence.source_code == "A"


def test_firmoteka_legal_form_object_and_string_normalize_without_registration_freshness():
    base = {
        "requested_inn": "0100000614",
        "rendered_inn": "0100000614",
        "registration_date": "2022-01-19",
        "fns_egrul_as_of": "2026-09-16",
    }
    object_facts = normalize_firmoteka_projection(
        {**base, "legal_form": {"code": "12300", "name": "ООО"}},
        company_id=1,
        snapshot_identity="snapshot:one",
        retrieved_at=NOW,
    )
    string_facts = normalize_firmoteka_projection(
        {**base, "legal_form": "ООО"},
        company_id=1,
        snapshot_identity="snapshot:two",
        retrieved_at=NOW,
    )
    object_form = next(item for item in object_facts if item.field_key == "legal_form")
    string_form = next(item for item in string_facts if item.field_key == "legal_form")
    assert object_form.value == {"code": "12300", "name": "ООО"}
    assert string_form.value == {"code": None, "name": "ООО"}
    assert object_form.source_data_date.isoformat() == "2026-09-16"
    assert object_form.source_data_date.isoformat() != base["registration_date"]


def test_resolver_is_exact_read_only_and_public_hides_company_id():
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT current_database()")) != "kontragent"
    with Session(engine) as session:
        company = Company(inn="7701234567", name="Resolver", entity_type="legal")
        session.add(company)
        session.flush()
        before = session.scalar(sa.select(sa.func.count()).select_from(Company))
        internal = resolve_company_by_inn(session, "7701234567")
        repeated = resolve_company_by_inn(session, "7701234567")
        public = resolve_company_by_inn(session, "7701234567", audience=Audience.PUBLIC)
        missing = resolve_company_by_inn(session, "7701234568")
        after = session.scalar(sa.select(sa.func.count()).select_from(Company))
        assert internal.company_id == company.id
        assert repeated.company_id == internal.company_id
        assert public.company_id is None
        assert public.state.value == "RESOLVED"
        assert missing.state.value == "NOT_FOUND"
        assert before == after
        assert not session.new and not session.dirty and not session.deleted
        session.rollback()


def test_persisted_fact_identity_survives_selected_value_and_source_change():
    with Session(engine) as session:
        company = Company(inn="7801234567", name="Stable fact", entity_type="legal")
        session.add(company)
        session.flush()
        first = select_semantic_facts((_candidate(company_id=company.id, value="1"),))[0]
        changed = select_semantic_facts(
            (
                _candidate(
                    company_id=company.id,
                    value="2",
                    source="OFFICIAL",
                    source_class=EvidenceSourceClass.OFFICIAL_PRIMARY,
                    rights=FactRights.PUBLIC,
                ),
            )
        )[0]
        persist_semantic_facts(session, (first,))
        session.flush()
        persist_semantic_facts(session, (changed,))
        session.flush()
        rows = tuple(
            session.scalars(
                sa.select(CompanySemanticFact).where(
                    CompanySemanticFact.company_id == company.id
                )
            )
        )
        assert len(rows) == 1
        assert rows[0].fact_ref == first.fact_ref == changed.fact_ref
        assert rows[0].selected_evidence["value"] == "2"
        assert rows[0].selected_evidence["source_code"] == "OFFICIAL"
        assert rows[0].evidence_history[0]["value"] == "1"
        assert rows[0].is_current is True
        session.rollback()


def test_public_and_authenticated_filters_preserve_revision_and_rights():
    facts = select_semantic_facts(
        (
            _candidate(value="bridge-only"),
            SemanticCandidate(
                **{
                    **_candidate(
                        value="public",
                        source="OFFICIAL",
                        source_class=EvidenceSourceClass.OFFICIAL_PRIMARY,
                        rights=FactRights.PUBLIC,
                    ).model_dump(),
                    "field_key": "EXPENSES",
                }
            ),
        )
    )
    from app.contracts.company_view_v1 import CompanyViewModelV1, CompanyViewSectionV1

    view = CompanyViewModelV1(
        revision="cv1:" + "a" * 64,
        generated_at=NOW,
        audience=Audience.INTERNAL,
        company_id=1,
        inn="7701234567",
        sections=(CompanyViewSectionV1(section_key="finances", state=DataState.FOUND, facts=facts),),
    )
    public = filter_company_view(view, audience=Audience.PUBLIC)
    authenticated = filter_company_view(view, audience=Audience.AUTHENTICATED)
    assert public.company_id is None
    assert len(public.sections[0].facts) == 1
    assert len(authenticated.sections[0].facts) == 2
    assert public.revision == authenticated.revision == view.revision
    assert "company_id" not in public.model_dump(mode="json", exclude_none=True)

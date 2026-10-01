from __future__ import annotations

import json
from datetime import UTC, date, datetime
from uuid import NAMESPACE_DNS, uuid5

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.contracts.company_view_v1 import (
    Audience,
    CompanyViewModelV1,
    CompanyViewSectionV1,
    DataState,
    EvidenceSourceClass,
    FactRights,
    Freshness,
)
from app.services.company_view_service import (
    SECTION_KEYS,
    SemanticCandidate,
    _connection_candidates,
    _safe_cbr_warning_value,
    _safe_inspection_value,
    filter_company_view,
    semantic_field_policy,
    select_semantic_facts,
)
from public_app.contracts import (
    Freshness as PublicFreshness,
    PublicCompanyViewV1,
    PublicFactSource,
    PublicProjection,
    PublicViewFact,
    PublicViewSection,
)
from public_app.main import create_app
from scripts.export_public_release import _public_company_view
from tests.public_test_support import projection


NOW = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)


def _candidate(
    *,
    section_key: str = "inspections",
    field_key: str = "availability",
    value=None,
    source_code: str = "ERKNM",
    source_class: EvidenceSourceClass = EvidenceSourceClass.OFFICIAL_API_OPEN_DATA,
    state: DataState = DataState.FOUND,
    freshness: Freshness = Freshness.CURRENT,
    item_identity: str = "",
) -> SemanticCandidate:
    return SemanticCandidate(
        company_id=1,
        section_key=section_key,
        field_key=field_key,
        value={"ok": True} if value is None and state == DataState.FOUND else value,
        source_code=source_code,
        source_class=source_class,
        evidence_identity=f"{source_code}:{section_key}:{field_key}:{item_identity or 'one'}",
        source_ref="https://example.test/source",
        source_data_date=date(2026, 10, 1),
        retrieved_at=NOW,
        confidence=1.0,
        freshness=freshness,
        rights=FactRights.PUBLIC,
        state=state,
        item_identity=item_identity,
    )


def test_company_view_contract_contains_required_public_card_domains():
    required = {
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
        "courts",
        "bankruptcy",
        "enforcement",
        "licenses",
        "procurement",
        "restrictions",
        "inspections",
        "connections",
        "risk",
        "summary",
        "source_coverage",
        "freshness",
    }
    assert required <= set(SECTION_KEYS)


def test_source_authority_policy_keeps_official_above_authorized_bridge():
    official = _candidate(
        section_key="identity",
        field_key="name",
        value="ООО ТЕСТ",
        source_code="MASTER_REGISTRY",
        source_class=EvidenceSourceClass.OFFICIAL_PRIMARY,
    )
    bridge = _candidate(
        section_key="identity",
        field_key="name",
        value="ООО ТЕСТ",
        source_code="FIRMOTEKA_AUTHORIZED_BRIDGE",
        source_class=EvidenceSourceClass.AUTHORIZED_BRIDGE,
    )
    fact = select_semantic_facts((bridge, official), observed_at=NOW)[0]

    assert fact.selected_evidence.source_code == "MASTER_REGISTRY"
    assert fact.selected_evidence.source_class == EvidenceSourceClass.OFFICIAL_PRIMARY
    assert tuple(item.source_code for item in fact.alternative_evidence) == (
        "FIRMOTEKA_AUTHORIZED_BRIDGE",
    )
    assert fact.state == DataState.FOUND

    restrictions = semantic_field_policy("restrictions", "warning")
    inspections = semantic_field_policy("inspections", "availability")
    assert restrictions.official_sources == ("CBR_WARNING_LIST",)
    assert restrictions.bridge_sources == ()
    assert inspections.official_sources == ("ERKNM",)
    assert inspections.not_applicable_allowed is True


def test_stale_unavailable_unknown_and_not_applicable_remain_distinct():
    stale = select_semantic_facts(
        (_candidate(field_key="inspection", value={"registry_number": "1"}, freshness=Freshness.STALE),),
        observed_at=NOW,
    )[0]
    unavailable = select_semantic_facts(
        (
            _candidate(
                value=None,
                state=DataState.SOURCE_UNAVAILABLE,
                freshness=Freshness.UNKNOWN,
            ),
        ),
        observed_at=NOW,
    )[0]
    unknown = select_semantic_facts(
        (
            _candidate(
                value=None,
                state=DataState.UNKNOWN,
                freshness=Freshness.UNKNOWN,
            ),
        ),
        observed_at=NOW,
    )[0]
    not_applicable = select_semantic_facts(
        (
            _candidate(
                value=None,
                state=DataState.NOT_APPLICABLE,
                freshness=Freshness.UNKNOWN,
            ),
        ),
        observed_at=NOW,
    )[0]

    assert stale.state == DataState.STALE_DATA
    assert unavailable.state == DataState.SOURCE_UNAVAILABLE
    assert unknown.state == DataState.UNKNOWN
    assert not_applicable.state == DataState.NOT_APPLICABLE

    with pytest.raises(ValidationError, match="NOT_APPLICABLE is not allowed"):
        _candidate(
            section_key="identity",
            field_key="name",
            value=None,
            source_code="MASTER_REGISTRY",
            source_class=EvidenceSourceClass.OFFICIAL_PRIMARY,
            state=DataState.NOT_APPLICABLE,
            freshness=Freshness.UNKNOWN,
        )


def test_official_domain_whitelists_do_not_forward_raw_or_personal_payloads():
    inspection = _safe_inspection_value(
        {
            "erpid": "ERKNM-1",
            "status": "Завершено",
            "kind_control": "Федеральный",
            "kind_knm": "Проверка",
            "kno_organization": "Контрольный орган",
            "start_date": date(2026, 9, 1),
            "end_date": date(2026, 9, 2),
            "object_address": "Публичный адрес объекта",
            "risk_category": "Средний риск",
            "result_text": "Результат опубликован",
            "warning_caption": "Предупреждение",
            "inspection_attributes": {"passport": "NEVER-PUBLIC"},
            "inspectors": [{"phone": "NEVER-PUBLIC"}],
            "subject_attributes": {"home_address": "NEVER-PUBLIC"},
        }
    )
    warning = _safe_cbr_warning_value(
        {
            "name": "ООО ПРИМЕР",
            "entry_date": date(2026, 8, 1),
            "update_date": date(2026, 9, 1),
            "signs": ["Признак из официального списка"],
            "regions": ["Москва"],
            "liquidation_status": "Действует",
            "org_type": "Организация",
            "raw_payload": {"passport": "NEVER-PUBLIC"},
            "sites": [{"token": "NEVER-PUBLIC"}],
            "address": "Не требуется в semantic warning",
        }
    )

    serialized = json.dumps({"inspection": inspection, "warning": warning}, default=str)
    assert "NEVER-PUBLIC" not in serialized
    for forbidden in (
        "inspection_attributes",
        "inspectors",
        "subject_attributes",
        "raw_payload",
        "passport",
        "token",
    ):
        assert forbidden not in serialized


def test_connections_are_derived_from_semantic_relations_not_provider_payloads():
    manager = _candidate(
        section_key="management",
        field_key="manager",
        value={
            "name": "Иванов Иван Иванович",
            "position": "Директор",
            "passport": "provider-only",
        },
        source_code="FIRMOTEKA_AUTHORIZED_BRIDGE",
        source_class=EvidenceSourceClass.AUTHORIZED_BRIDGE,
        item_identity="иванов иван иванович",
    )
    relation = _connection_candidates((manager,))[0]

    assert relation.section_key == "connections"
    assert relation.field_key == "person_relation"
    assert relation.value == {
        "name": "Иванов Иван Иванович",
        "relation": "MANAGER",
        "position": "Директор",
        "share": None,
        "current_status": None,
    }
    assert "passport" not in relation.value
    assert relation.source_code == "FIRMOTEKA_AUTHORIZED_BRIDGE"


def _ref(kind: str, seed: str) -> str:
    return f"{kind}:{uuid5(NAMESPACE_DNS, seed)}"


def _source(name: str, source_class: str) -> PublicFactSource:
    return PublicFactSource(
        name=name,
        source_class=source_class,
        reference="https://example.test/source",
        source_data_date=date(2026, 10, 1),
        retrieved_at=NOW,
        confidence=1.0,
        freshness=PublicFreshness.CURRENT,
    )


def _fact(
    section: str,
    field: str,
    value,
    *,
    state: str = "Сведения найдены",
    source_name: str = "Официальный источник",
    source_class: str = "Официальные открытые данные",
    limitations: tuple[str, ...] = (),
) -> PublicViewFact:
    return PublicViewFact(
        fact_ref=_ref("fact", f"{section}:{field}"),
        item_ref=_ref("item", f"{section}:{field}"),
        field_key=field,
        label=field,
        value=value,
        state=state,
        source=_source(source_name, source_class),
        limitations=limitations,
    )


class _Repository:
    def __init__(self, item: PublicProjection):
        self.item = item

    def get_company(self, inn):
        return self.item if inn == self.item.company.inn else None

    def ready(self):
        return True, self.item.publication.release_id, 1

    def search(self, _query, limit=20):
        return [self.item][:limit]

    def sitemap_rows(self):
        return []


def test_public_api_and_card_render_same_semantic_domains_without_internal_leakage():
    base = projection()
    sections = (
        PublicViewSection(
            section_key="connections",
            title="Связи",
            state="Сведения найдены",
            items=(
                _fact(
                    "connections",
                    "person_relation",
                    {
                        "name": "Иванов Иван Иванович",
                        "relation": "MANAGER",
                        "position": "Директор",
                        "share": None,
                    },
                    source_name="Firmoteka · авторизованный вторичный источник",
                    source_class="Авторизованный вторичный источник",
                ),
            ),
        ),
        PublicViewSection(
            section_key="courts",
            title="Суды",
            state="Источник временно недоступен",
            items=(
                _fact(
                    "courts",
                    "availability",
                    None,
                    state="Источник временно недоступен",
                    source_name="Официальный портал судов общей юрисдикции Москвы",
                    limitations=("Отрицательный вывод запрещён.",),
                ),
            ),
        ),
        PublicViewSection(
            section_key="bankruptcy",
            title="Банкротство",
            state="Сведения пока не проверены",
        ),
        PublicViewSection(
            section_key="procurement",
            title="Закупки и РНП",
            state="Сведения пока не проверены",
        ),
        PublicViewSection(
            section_key="restrictions",
            title="Ограничения и предупреждения",
            state="Сведения найдены",
            items=(
                _fact(
                    "restrictions",
                    "warning",
                    {
                        "name": "ООО ТЕСТ",
                        "signs": ["Признак из официального списка"],
                    },
                    source_name="Предупредительный список Банка России",
                ),
            ),
        ),
        PublicViewSection(
            section_key="inspections",
            title="Проверки и контрольные мероприятия",
            state="Сведения найдены",
            items=(
                _fact(
                    "inspections",
                    "availability",
                    {"record_count": 2, "loaded_through": "2026-10-01"},
                    source_name="Единый реестр контрольных (надзорных) мероприятий",
                ),
            ),
        ),
    )
    view = PublicCompanyViewV1(
        revision="cv1:" + "d" * 64,
        generated_at=NOW,
        inn=base.company.inn,
        sections=sections,
        links={
            "public_card": f"/companies/{base.company.inn}",
            "public_api": f"/api/company/{base.company.inn}",
        },
    )
    item = base.model_copy(update={"company_view": view})
    web = TestClient(create_app(_Repository(item)))

    api = web.get(f"/api/company/{item.company.inn}")
    card = web.get(f"/companies/{item.company.inn}")
    assert api.status_code == card.status_code == 200
    payload = api.json()
    by_key = {section["section_key"]: section for section in payload["view"]["sections"]}

    assert payload["view"]["revision"] == view.revision
    assert by_key["courts"]["state"] == "Источник временно недоступен"
    assert by_key["bankruptcy"]["state"] == "Сведения пока не проверены"
    assert by_key["procurement"]["state"] == "Сведения пока не проверены"
    assert by_key["restrictions"]["items"][0]["source"]["name"] == "Предупредительный список Банка России"
    assert by_key["connections"]["items"][0]["source"]["source_class"] == "Авторизованный вторичный источник"

    for text in (
        "Связи",
        "Иванов Иван Иванович",
        "Суды",
        "Источник временно недоступен",
        "Банкротство",
        "Закупки и РНП",
        "Предупредительный список Банка России",
        "Контрольные мероприятия",
    ):
        assert text in card.text

    serialized = json.dumps(payload, ensure_ascii=False)
    for forbidden in (
        "company_id",
        "raw_payload",
        "parser",
        "FIRMOTEKA_AUTHORIZED_BRIDGE",
        "passport",
        "home_address",
    ):
        assert forbidden not in serialized
        assert forbidden not in card.text


def test_card_copy_action_is_self_hosted_and_csp_remains_closed_to_inline_scripts():
    base = projection()
    web = TestClient(create_app(_Repository(base)))
    card = web.get(f"/companies/{base.company.inn}")

    assert card.status_code == 200
    assert 'data-copy-button' in card.text
    assert 'data-copy-value=' in card.text
    assert '<script src="/static/company.js" defer></script>' in card.text
    csp = card.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "'unsafe-inline'" not in csp
    script = web.get("/static/company.js")
    assert script.status_code == 200
    assert "navigator.clipboard" in script.text


def test_public_export_preserves_limiting_section_state_with_state_item():
    base = projection()
    fact = select_semantic_facts(
        (
            _candidate(
                section_key="courts",
                field_key="availability",
                value=None,
                source_code="MOSCOW_COURTS_OFFICIAL",
                source_class=EvidenceSourceClass.OFFICIAL_PRIMARY,
                state=DataState.SOURCE_UNAVAILABLE,
                freshness=Freshness.UNKNOWN,
            ),
        ),
        observed_at=NOW,
    )[0]
    internal = CompanyViewModelV1(
        revision="cv1:" + "e" * 64,
        generated_at=NOW,
        audience=Audience.INTERNAL,
        company_id=1,
        inn=base.company.inn,
        sections=(
            CompanyViewSectionV1(
                section_key="courts",
                state=DataState.SOURCE_UNAVAILABLE,
                facts=(fact,),
            ),
        ),
    )
    semantic_public = filter_company_view(internal, audience=Audience.PUBLIC)
    public_view = _public_company_view(
        semantic_public,
        company_id=1,
        risk=base.risk,
        summary=base.summary,
        sources=base.sources,
    )
    courts = public_view.section("courts")

    assert courts is not None
    assert courts.state == "Источник временно недоступен"
    assert courts.items[0].state == "Источник временно недоступен"
    assert courts.items[0].value is None

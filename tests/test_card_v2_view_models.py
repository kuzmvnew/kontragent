import inspect
from datetime import date

import pytest
from pydantic import ValidationError

from app.contracts import public_card_projection as projection_module
from app.contracts.card_v2 import (
    ActionViewModel,
    CardAction,
    CardActionKind,
    CardV2ViewModel,
    PublicEvidenceReference,
    PublicUIState,
)
from app.contracts.public_card_projection import (
    PublicCardProjection,
    PublicCompanyHeaderProjection,
    PublicRiskProjection,
    PublicSummaryProjection,
)
from app.contracts.risk_v3 import (
    Applicability,
    Execution,
    Freshness,
    Observation,
    ResolutionState,
    ScopeCompleteness,
)
from app.services import card_v2_view_model_service as service_module
from app.services.card_v2_view_model_service import (
    CardV2ViewModelService,
    INTERNAL_STATE_TO_PUBLIC_UI_STATE,
    build_card_actions,
    build_public_card_actions,
    project_coverage_item,
    project_public_evidence,
    translate_public_ui_state,
)


@pytest.mark.parametrize(
    ("internal", "expected"),
    [
        ("FOUND", PublicUIState.FOUND),
        ("NOT_FOUND", PublicUIState.NOT_FOUND),
        ("NOT_APPLICABLE", PublicUIState.NOT_APPLICABLE),
        ("PARTIAL", PublicUIState.PARTIAL),
        ("SOURCE_UNAVAILABLE", PublicUIState.SOURCE_UNAVAILABLE),
        ("STALE", PublicUIState.STALE),
        ("UNKNOWN", PublicUIState.UNKNOWN),
        ("CONFLICTING_EVIDENCE", PublicUIState.CONFLICTING_EVIDENCE),
        ("ERROR", PublicUIState.ERROR),
    ],
)
def test_all_public_states_have_an_internal_mapping(internal, expected):
    assert translate_public_ui_state(internal) == expected


def test_state_translation_accepts_internal_enums_and_is_order_independent():
    values = (
        Observation.FOUND,
        Execution.CHECKED,
        Freshness.STALE,
        ScopeCompleteness.PARTIAL,
        ResolutionState.UNRESOLVED,
    )

    assert translate_public_ui_state(*values) == PublicUIState.STALE
    assert translate_public_ui_state(*reversed(values)) == PublicUIState.STALE


def test_unknown_or_new_internal_state_fails_closed():
    assert translate_public_ui_state() == PublicUIState.UNKNOWN
    assert translate_public_ui_state("A_FUTURE_INTERNAL_STATE") == PublicUIState.UNKNOWN
    assert translate_public_ui_state(Observation.UNKNOWN) == PublicUIState.UNKNOWN


def test_not_applicable_is_preserved_separately_from_unknown():
    assert (
        translate_public_ui_state(
            Applicability.NOT_APPLICABLE,
            Observation.UNKNOWN,
        )
        == PublicUIState.NOT_APPLICABLE
    )
    assert translate_public_ui_state(Observation.UNKNOWN) == PublicUIState.UNKNOWN
    assert PublicUIState.NOT_APPLICABLE != PublicUIState.UNKNOWN


def test_conflicting_observations_do_not_become_a_clean_result():
    assert (
        translate_public_ui_state(Observation.FOUND, Observation.NOT_FOUND)
        == PublicUIState.CONFLICTING_EVIDENCE
    )


def test_mapping_is_immutable():
    with pytest.raises(TypeError):
        INTERNAL_STATE_TO_PUBLIC_UI_STATE["NEW"] = PublicUIState.ERROR


def test_public_evidence_contains_only_approved_fields():
    evidence = project_public_evidence(
        source_name="ФНС России",
        date=date(2026, 9, 20),
        description="Проверка задолженности по опубликованным данным.",
        internal_states=(Execution.CHECKED, Observation.NOT_FOUND),
    )

    assert evidence.model_dump(mode="json") == {
        "source_name": "ФНС России",
        "date": "2026-09-20",
        "description": "Проверка задолженности по опубликованным данным.",
        "status": "NOT_FOUND",
    }
    assert not (
        {"source_ref", "evidence_ref", "raw_payload", "metadata"}
        & set(PublicEvidenceReference.model_fields)
    )


def test_public_evidence_rejects_raw_provenance_fields():
    with pytest.raises(ValidationError):
        PublicEvidenceReference(
            source_name="ФНС России",
            date=date(2026, 9, 20),
            description="Публичное основание",
            status=PublicUIState.FOUND,
            raw_payload={"secret": "internal"},
        )


def test_stale_evidence_is_not_projected_as_found():
    evidence = project_public_evidence(
        source_name="Официальный источник",
        date=date(2025, 1, 1),
        description="Данные требуют обновления.",
        internal_states=(Observation.FOUND, Freshness.STALE),
    )

    assert evidence.status == PublicUIState.STALE


def test_not_applicable_evidence_preserves_state_and_explanation():
    evidence = project_public_evidence(
        source_name="Официальный источник",
        date=date(2026, 9, 20),
        description="Проверка не применяется к виду деятельности компании.",
        internal_states=(Applicability.NOT_APPLICABLE, Observation.UNKNOWN),
    )

    assert evidence.status == PublicUIState.NOT_APPLICABLE
    assert evidence.description == (
        "Проверка не применяется к виду деятельности компании."
    )


def test_coverage_item_is_capability_state_not_risk():
    item = project_coverage_item(
        capability="courts",
        title="Арбитражные дела",
        internal_states=(Execution.CHECKED, ScopeCompleteness.PARTIAL),
        limitation="Проверена только доступная часть источника.",
    )

    assert item.model_dump(mode="json") == {
        "capability": "courts",
        "title": "Арбитражные дела",
        "state": "PARTIAL",
        "limitation": "Проверена только доступная часть источника.",
    }
    assert not (
        {"risk", "risk_level", "score", "severity"}
        & set(type(item).model_fields)
    )


def test_not_applicable_coverage_preserves_limitation_reason():
    item = project_coverage_item(
        capability="sector_license",
        title="Отраслевая лицензия",
        internal_states=(Applicability.NOT_APPLICABLE, Observation.UNKNOWN),
        limitation="Для этой компании лицензия не требуется.",
    )

    assert item.state == PublicUIState.NOT_APPLICABLE
    assert item.limitation == "Для этой компании лицензия не требуется."
    with pytest.raises(ValidationError):
        project_coverage_item(
            capability="sector_license",
            title="Отраслевая лицензия",
            internal_states=(Applicability.NOT_APPLICABLE,),
        )


def test_actions_are_complete_ordered_and_ui_ready():
    view_model = build_card_actions(
        disabled_reasons={CardActionKind.WATCH: "Нужна авторизация"}
    )

    assert [item.action for item in view_model.actions] == [
        CardActionKind.GET_REPORT,
        CardActionKind.WATCH,
        CardActionKind.SAVE,
    ]
    assert [item.label for item in view_model.actions] == [
        "Получить отчёт",
        "Наблюдать",
        "Сохранить",
    ]
    assert view_model.actions[1].enabled is False
    assert view_model.actions[1].disabled_reason == "Нужна авторизация"


def test_action_view_model_rejects_duplicates():
    action = CardAction(
        action=CardActionKind.SAVE,
        label="Сохранить",
    )
    with pytest.raises(ValidationError):
        ActionViewModel(actions=(action, action))


def _public_card_projection() -> PublicCardProjection:
    evidence = project_public_evidence(
        source_name="ФНС России",
        date=date(2026, 9, 20),
        description="Публичные данные",
        internal_states=(Observation.FOUND,),
    )
    coverage_item = project_coverage_item(
        capability="registration",
        title="Регистрационные сведения",
        internal_states=(Observation.FOUND,),
    )

    return PublicCardProjection(
        company_header=PublicCompanyHeaderProjection(
            name='ООО "Пример"',
            inn="7701234567",
            kpp="770101001",
            ogrn="1027700123456",
            legal_status="Действующая организация",
            as_of=date(2026, 9, 20),
        ),
        summary=PublicSummaryProjection(
            state=PublicUIState.FOUND,
            title="Проверка выполнена",
            description="Доступные публичные сведения собраны.",
        ),
        risk=PublicRiskProjection(
            state=PublicUIState.FOUND,
            title="Выявленные факторы",
            description="Показаны факты из публичной проекции.",
            evidence=(evidence,),
        ),
        coverage_items=(coverage_item,),
        evidence=(evidence,),
        actions=build_public_card_actions(),
    )


def test_public_projection_is_adapted_to_complete_card_view_model():
    projection = _public_card_projection()
    card = CardV2ViewModelService.build(projection)

    assert isinstance(card, CardV2ViewModel)
    assert card.company_header.inn == "7701234567"
    assert len(card.actions.actions) == 3
    assert card.company_header is not projection.company_header
    assert isinstance(card.evidence.items[0], PublicEvidenceReference)
    assert card.evidence.items[0] is not projection.evidence[0]


def test_not_applicable_survives_public_projection_adapter_with_reason():
    explanation = "Проверка не применяется к виду деятельности компании."
    limitation = "Для этой компании лицензия не требуется."
    evidence = project_public_evidence(
        source_name="Официальный источник",
        date=date(2026, 9, 20),
        description=explanation,
        internal_states=(Applicability.NOT_APPLICABLE, Observation.UNKNOWN),
    )
    coverage = project_coverage_item(
        capability="sector_license",
        title="Отраслевая лицензия",
        internal_states=(Applicability.NOT_APPLICABLE, Observation.UNKNOWN),
        limitation=limitation,
    )
    base = _public_card_projection()
    projection = PublicCardProjection(
        company_header=base.company_header,
        summary=PublicSummaryProjection(
            state=PublicUIState.NOT_APPLICABLE,
            title="Проверка не применяется",
            description=explanation,
        ),
        risk=PublicRiskProjection(
            state=PublicUIState.NOT_APPLICABLE,
            title="Оценка не применяется",
            description=explanation,
            evidence=(evidence,),
        ),
        coverage_items=(coverage,),
        evidence=(evidence,),
        actions=base.actions,
    )

    card = CardV2ViewModelService.build(projection)

    assert card.summary.state == PublicUIState.NOT_APPLICABLE
    assert card.summary.description == explanation
    assert card.risk.state == PublicUIState.NOT_APPLICABLE
    assert card.evidence.items[0].status == PublicUIState.NOT_APPLICABLE
    assert card.evidence.items[0].description == explanation
    assert card.coverage.items[0].state == PublicUIState.NOT_APPLICABLE
    assert card.coverage.items[0].limitation == limitation


def test_card_service_rejects_non_public_projection_inputs():
    with pytest.raises(TypeError, match="PublicCardProjection only"):
        CardV2ViewModelService.build(object())


def test_public_projection_rejects_source_payload():
    payload = _public_card_projection().model_dump()
    payload["source_payload"] = {"raw": "not public"}

    with pytest.raises(ValidationError):
        PublicCardProjection.model_validate(payload)


def test_card_service_has_no_internal_model_dependency():
    source = inspect.getsource(service_module)
    for forbidden_dependency in (
        "app.contracts.risk_v3",
        "ResolvedCheckResult",
        "app.models",
        "app.providers",
    ):
        assert forbidden_dependency not in source

    projection_source = inspect.getsource(projection_module)
    assert "app.contracts.card_v2" not in projection_source

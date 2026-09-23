from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from app.contracts.card_v2 import (
    ActionViewModel,
    CardAction,
    CardActionKind,
    CardV2ViewModel,
    CompanyHeaderViewModel,
    CoverageViewModel,
    EvidenceViewModel,
    PublicEvidenceReference,
    PublicUIState,
    RiskViewModel,
    SummaryViewModel,
)
from app.contracts.risk_v3 import (
    Applicability,
    Execution,
    Freshness,
    Observation,
    ResolvedCheckResult,
    ResolutionState,
    ScopeCompleteness,
    TemporalKind,
)
from app.services.card_v2_view_model_service import (
    INTERNAL_STATE_TO_PUBLIC_UI_STATE,
    build_card_actions,
    project_coverage_item,
    project_public_evidence,
    translate_public_ui_state,
    translate_resolved_check_state,
)


@pytest.mark.parametrize(
    ("internal", "expected"),
    [
        ("FOUND", PublicUIState.FOUND),
        ("NOT_FOUND", PublicUIState.NOT_FOUND),
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


def test_conflicting_observations_do_not_become_a_clean_result():
    assert (
        translate_public_ui_state(Observation.FOUND, Observation.NOT_FOUND)
        == PublicUIState.CONFLICTING_EVIDENCE
    )


def test_mapping_is_immutable():
    with pytest.raises(TypeError):
        INTERNAL_STATE_TO_PUBLIC_UI_STATE["NEW"] = PublicUIState.ERROR


def test_resolved_check_translation_preserves_internal_states():
    check = ResolvedCheckResult(
        check_ref="check:tax-debt",
        capability_code="tax_debt",
        fact_identity="tax-debt:current",
        company_id=42,
        applicability=Applicability.APPLICABLE,
        observation=Observation.FOUND,
        execution=Execution.CHECKED,
        freshness=Freshness.STALE,
        scope=ScopeCompleteness.COMPLETE,
        temporal_kind=TemporalKind.CURRENT_STATE,
        resolution_state=ResolutionState.UNRESOLVED,
        selected_evidence_refs=("evidence:internal:1",),
        candidate_refs=("candidate:1",),
        source_refs=("source:internal:1",),
        fact_payload={"amount": "10.00"},
        checked_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        resolution_policy_version="test-v1",
    )
    before = check.model_dump()

    assert translate_resolved_check_state(check) == PublicUIState.STALE
    assert check.model_dump() == before


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


def test_complete_card_view_model_composes_the_six_component_models():
    evidence = PublicEvidenceReference(
        source_name="ФНС России",
        date=date(2026, 9, 20),
        description="Публичные данные",
        status=PublicUIState.FOUND,
    )
    coverage_item = project_coverage_item(
        capability="registration",
        title="Регистрационные сведения",
        internal_states=(Observation.FOUND,),
    )

    card = CardV2ViewModel(
        company_header=CompanyHeaderViewModel(
            name='ООО "Пример"',
            inn="7701234567",
            kpp="770101001",
            ogrn="1027700123456",
            legal_status="Действующая организация",
            as_of=date(2026, 9, 20),
        ),
        summary=SummaryViewModel(
            state=PublicUIState.FOUND,
            title="Проверка выполнена",
            description="Доступные публичные сведения собраны.",
        ),
        risk=RiskViewModel(
            state=PublicUIState.FOUND,
            title="Выявленные факторы",
            description="Показаны факты из публичной проекции.",
            evidence=(evidence,),
        ),
        coverage=CoverageViewModel(items=(coverage_item,)),
        evidence=EvidenceViewModel(items=(evidence,)),
        actions=build_card_actions(),
    )

    assert card.company_header.inn == "7701234567"
    assert len(card.actions.actions) == 3

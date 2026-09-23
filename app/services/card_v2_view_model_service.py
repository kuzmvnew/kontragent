"""Projection helpers for the public Company Card v2 view boundary.

The module translates already-computed domain/public-projection values.  It
does not call sources and does not calculate risk, summary, or coverage.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date as Date
from enum import Enum
from types import MappingProxyType

from app.contracts.card_v2 import (
    ActionViewModel,
    CardAction,
    CardActionKind,
    CardV2ViewModel,
    CompanyHeaderViewModel,
    CoverageItem,
    CoverageViewModel,
    EvidenceViewModel,
    PublicEvidenceReference,
    PublicUIState,
    RiskViewModel,
    SummaryViewModel,
)
from app.contracts.public_card_projection import (
    PublicCardActionProjection,
    PublicCardProjection,
    PublicCoverageItemProjection,
    PublicEvidenceProjection,
)


InternalState = Enum | str | None


# One explicit, immutable translation table keeps internal source state
# vocabularies out of Card v2. Qualifier states such as CHECKED, CURRENT and
# COMPLETE intentionally have no direct UI state and therefore fall back to
# UNKNOWN when supplied without an outcome.
INTERNAL_STATE_TO_PUBLIC_UI_STATE: Mapping[str, PublicUIState] = MappingProxyType(
    {
        "FOUND": PublicUIState.FOUND,
        "NOT_FOUND": PublicUIState.NOT_FOUND,
        "NOT_APPLICABLE": PublicUIState.NOT_APPLICABLE,
        "PARTIAL": PublicUIState.PARTIAL,
        "PARTIAL_COVERAGE": PublicUIState.PARTIAL,
        "SOURCE_UNAVAILABLE": PublicUIState.SOURCE_UNAVAILABLE,
        "UNAVAILABLE": PublicUIState.SOURCE_UNAVAILABLE,
        "TIMEOUT": PublicUIState.SOURCE_UNAVAILABLE,
        "STALE": PublicUIState.STALE,
        "UNKNOWN": PublicUIState.UNKNOWN,
        "NOT_CHECKED": PublicUIState.UNKNOWN,
        "APPLICABILITY_UNKNOWN": PublicUIState.UNKNOWN,
        "FRESHNESS_UNKNOWN": PublicUIState.UNKNOWN,
        "SCOPE_UNKNOWN": PublicUIState.UNKNOWN,
        "UNRESOLVED": PublicUIState.UNKNOWN,
        "CONFLICTING_EVIDENCE": PublicUIState.CONFLICTING_EVIDENCE,
        "PARSING_ERROR": PublicUIState.ERROR,
        "ERROR": PublicUIState.ERROR,
        "FAILED": PublicUIState.ERROR,
    }
)


_PUBLIC_STATE_PRECEDENCE = (
    PublicUIState.CONFLICTING_EVIDENCE,
    PublicUIState.ERROR,
    PublicUIState.NOT_APPLICABLE,
    PublicUIState.SOURCE_UNAVAILABLE,
    PublicUIState.STALE,
    PublicUIState.PARTIAL,
    PublicUIState.UNKNOWN,
    PublicUIState.FOUND,
    PublicUIState.NOT_FOUND,
)


def _state_token(value: InternalState) -> str | None:
    if value is None:
        return None
    raw = value.value if isinstance(value, Enum) else value
    return str(raw).strip().upper().replace("-", "_").replace(" ", "_")


def translate_public_ui_state(*internal_states: InternalState) -> PublicUIState:
    """Translate any internal state axes into one fail-closed public UI state.

    Precedence is independent of argument order.  A limitation state masks a
    nominal observation, so stale/partial/unavailable data can never be shown
    as a clean FOUND or NOT_FOUND result.
    """

    translated = {
        state
        for value in internal_states
        if (token := _state_token(value)) is not None
        if (state := INTERNAL_STATE_TO_PUBLIC_UI_STATE.get(token)) is not None
    }
    if {PublicUIState.FOUND, PublicUIState.NOT_FOUND} <= translated:
        return PublicUIState.CONFLICTING_EVIDENCE
    return next(
        (state for state in _PUBLIC_STATE_PRECEDENCE if state in translated),
        PublicUIState.UNKNOWN,
    )


def project_public_evidence(
    *,
    source_name: str,
    date: Date | None,
    description: str,
    internal_states: Iterable[InternalState],
) -> PublicEvidenceProjection:
    """Create the deliberately minimal public evidence projection."""

    return PublicEvidenceProjection(
        source_name=source_name,
        date=date,
        description=description,
        status=translate_public_ui_state(*internal_states),
    )


def project_coverage_item(
    *,
    capability: str,
    title: str,
    internal_states: Iterable[InternalState],
    limitation: str | None = None,
) -> PublicCoverageItemProjection:
    """Project capability coverage without deriving or labelling company risk."""

    return PublicCoverageItemProjection(
        capability=capability,
        title=title,
        state=translate_public_ui_state(*internal_states),
        limitation=limitation,
    )


_ACTION_LABELS: Mapping[CardActionKind, str] = MappingProxyType(
    {
        CardActionKind.GET_REPORT: "Получить отчёт",
        CardActionKind.WATCH: "Наблюдать",
        CardActionKind.SAVE: "Сохранить",
    }
)


def build_public_card_actions(
    *,
    disabled_reasons: Mapping[CardActionKind | str, str] | None = None,
) -> tuple[PublicCardActionProjection, ...]:
    """Return the complete public action projection in display order."""

    reasons = {
        CardActionKind(key): reason
        for key, reason in (disabled_reasons or {}).items()
    }
    return tuple(
        PublicCardActionProjection(
            action=kind,
            label=label,
            enabled=kind not in reasons,
            disabled_reason=reasons.get(kind),
        )
        for kind, label in _ACTION_LABELS.items()
    )


def build_card_actions(
    *,
    disabled_reasons: Mapping[CardActionKind | str, str] | None = None,
) -> ActionViewModel:
    """Return the complete, stable Card v2 action set in display order."""

    return ActionViewModel(
        actions=tuple(
            CardAction.model_validate(item.model_dump())
            for item in build_public_card_actions(
                disabled_reasons=disabled_reasons
            )
        )
    )


class CardV2ViewModelService:
    """Adapt the approved public projection to Card v2 component models."""

    @staticmethod
    def build(projection: PublicCardProjection) -> CardV2ViewModel:
        if not isinstance(projection, PublicCardProjection):
            raise TypeError("CardV2ViewModelService accepts PublicCardProjection only")

        header = CompanyHeaderViewModel.model_validate(
            projection.company_header.model_dump()
        )
        summary = SummaryViewModel.model_validate(projection.summary.model_dump())
        risk = RiskViewModel(
            **projection.risk.model_dump(exclude={"evidence"}),
            evidence=tuple(
                PublicEvidenceReference.model_validate(item.model_dump())
                for item in projection.risk.evidence
            ),
        )
        coverage = CoverageViewModel(
            items=tuple(
                CoverageItem.model_validate(item.model_dump())
                for item in projection.coverage_items
            )
        )
        evidence = EvidenceViewModel(
            items=tuple(
                PublicEvidenceReference.model_validate(item.model_dump())
                for item in projection.evidence
            )
        )
        actions = ActionViewModel(
            actions=tuple(
                CardAction.model_validate(item.model_dump())
                for item in projection.actions
            )
        )
        return CardV2ViewModel(
            company_header=header,
            summary=summary,
            risk=risk,
            coverage=coverage,
            evidence=evidence,
            actions=actions,
        )

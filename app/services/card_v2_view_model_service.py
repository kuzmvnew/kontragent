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
    CoverageItem,
    PublicEvidenceReference,
    PublicUIState,
)
from app.contracts.risk_v3 import ResolvedCheckResult


InternalState = Enum | str | None


# One explicit, immutable translation table keeps internal source state
# vocabularies out of Card v2. Qualifier states such as CHECKED, CURRENT and
# COMPLETE intentionally have no direct UI state and therefore fall back to
# UNKNOWN when supplied without an outcome.
INTERNAL_STATE_TO_PUBLIC_UI_STATE: Mapping[str, PublicUIState] = MappingProxyType(
    {
        "FOUND": PublicUIState.FOUND,
        "NOT_FOUND": PublicUIState.NOT_FOUND,
        "PARTIAL": PublicUIState.PARTIAL,
        "PARTIAL_COVERAGE": PublicUIState.PARTIAL,
        "SOURCE_UNAVAILABLE": PublicUIState.SOURCE_UNAVAILABLE,
        "UNAVAILABLE": PublicUIState.SOURCE_UNAVAILABLE,
        "TIMEOUT": PublicUIState.SOURCE_UNAVAILABLE,
        "STALE": PublicUIState.STALE,
        "UNKNOWN": PublicUIState.UNKNOWN,
        "NOT_CHECKED": PublicUIState.UNKNOWN,
        "NOT_APPLICABLE": PublicUIState.UNKNOWN,
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


def translate_resolved_check_state(check: ResolvedCheckResult) -> PublicUIState:
    """Translate Risk v3 check axes without modifying the internal check."""

    return translate_public_ui_state(
        check.resolution_state,
        check.execution,
        check.freshness,
        check.scope,
        check.observation,
    )


def project_public_evidence(
    *,
    source_name: str,
    date: Date | None,
    description: str,
    internal_states: Iterable[InternalState],
) -> PublicEvidenceReference:
    """Create the deliberately minimal evidence shape exposed to the UI."""

    return PublicEvidenceReference(
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
) -> CoverageItem:
    """Project capability coverage without deriving or labelling company risk."""

    return CoverageItem(
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


def build_card_actions(
    *,
    disabled_reasons: Mapping[CardActionKind | str, str] | None = None,
) -> ActionViewModel:
    """Return the complete, stable Card v2 action set in display order."""

    reasons = {
        CardActionKind(key): reason
        for key, reason in (disabled_reasons or {}).items()
    }
    actions = tuple(
        CardAction(
            action=kind,
            label=label,
            enabled=kind not in reasons,
            disabled_reason=reasons.get(kind),
        )
        for kind, label in _ACTION_LABELS.items()
    )
    return ActionViewModel(actions=actions)

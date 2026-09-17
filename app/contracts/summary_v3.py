from __future__ import annotations

from pydantic import Field

from app.contracts.decision import ContractModel


class SummarySourceDetailV3(ContractModel):
    source: str
    source_type: str
    date: str | None = None
    coverage: str
    rule: str | None = None
    calculation: str | None = None
    risk_points: float = 0


class SummaryV3(ContractModel):
    version: str = "summary-engine-3.1.1"
    risk_line: str
    coverage_line: str
    workflow_line: str = "Проверки завершены: не рассчитано"
    conclusion: str
    main_reasons: tuple[str, ...] = Field(max_length=3)
    positive_checks: tuple[str, ...]
    limitations: tuple[str, ...]
    recommendations: tuple[str, ...]
    source_details: tuple[SummarySourceDetailV3, ...]
    positive_conclusion_allowed: bool

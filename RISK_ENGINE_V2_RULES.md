# Risk Engine v2 rules

Version: `risk-engine-2.0.1` / `risk-rules-2.0.0`
Status: implementation foundation; product acceptance pending live data gates.

## Correctness changes

- Missing registration status is `NOT_CHECKED`, severity `NONE`, and is not completed.
- Finance uses `FINANCIAL_RESULT`, never `REGISTRATION_STATUS`.
- Partial/unavailable/stale coverage has severity `NONE` unless a separate factual signal exists.
- Overall states are `CRITICAL`, `HIGH`, `ATTENTION`, `NO_MATERIAL_RISKS`, `INSUFFICIENT_DATA`.
- A positive overall requires completion of mandatory registration, tax, bankruptcy, FSSP, CBR, finance and court checks.
- Active evidence-backed bankruptcy is critical. Intent, liquidation and completed procedures remain separate.
- FSSP found/not-found/unavailable are distinct states.

## Tax tiers

Thresholds are configurable product policy, not statutory limits:

- attention: debt at least 100,000 ₽ or at least 1% of valid revenue;
- high: debt at least 10,000,000 ₽ **and** at least 25% of valid revenue;
- an extreme ratio at or above 1,000% creates `DATA_QUALITY_REVIEW_REQUIRED`;
- an extreme ratio cannot by itself create high severity before raw values, units and periods are verified.

The numerator, denominator, periods, source dates and calculation remain in traceability details.

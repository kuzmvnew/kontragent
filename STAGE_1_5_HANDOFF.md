# STAGE 1.5 HANDOFF — Kontragent

Date: 2026-09-17
Status: READY TO START

## Baseline

Wave 1 is officially closed by `WAVE1_CLOSURE_DECISION.md`.

GitHub main at the start of this documentation sync was:

`79d9e3e9aae588e7df04af46c34d796853bf292f`

Commit message:

`Docs: officially close and accept Wave 1`

Before coding Stage 1.5, always re-fetch current `main`; do not assume this SHA remains current after documentation merge.

## Wave 1 final status

| Step | Source | Status |
|---|---|---|
| W1-001 | CBR Warning List | ACCEPTED / SIX GATES PASS |
| W1-002 | CBR FinOrg | ACCEPTED / SIX GATES PASS |
| W1-003 | FNS SME support recipients | ACCEPTED / SIX GATES PASS |
| W1-004 | Roszdravnadzor | ACCEPTED / SIX GATES PASS |
| W1-005 | Roskomnadzor | DEFERRED EXCEPTION / SOURCE-BLOCKED B/C; A/D/E/F verified |
| W1-006 | NOSTROY / NOPRIZ / SRO | ACCEPTED / SIX GATES PASS |

Wave 1 is CLOSED even though W1-005 B/C remains an external-source exception. Do not relabel B/C as success or `not_found`.

## Pre-Wave1 / existing source inventory

These sources already exist at least at code/data/product-foundation level and must not be blindly reimplemented:

| Source/block | Current documented state | Stage 1.5 action |
|---|---|---|
| Master Registry | ready | reuse |
| FNS Tax Debt | ACCEPTED / SIX GATES PASS on 2026-09-17 | reuse; protocol `docs/STAGE_1_5_A1_TAX_DEBT_ACCEPTANCE.md` |
| FNS Tax Offences | ACCEPTED / SIX GATES PASS on 2026-09-17 | reuse; protocol `docs/STAGE_1_5_A2_TAX_OFFENCE_ACCEPTANCE.md` |
| FNS Paid Taxes | connected | reuse |
| FNS Revenue & Expenses | connected | reuse |
| FNS Average Employees | connected | reuse |
| SME Registry | connected | reuse |
| SNR / SNRIP | completed | reuse |
| Disqualified Persons | completed | reuse |
| NPD | completed on-demand/cache | reuse |
| ERKNM | completed | reuse |
| RNP/EIS | deferred external access blocker | do not substitute with paid mirror by default |
| FSSP | product owner states it was developed earlier; current active implementation needs inventory | inventory current/history/local first |
| Fedresurs/EFРSB | product owner states it was developed earlier; current active implementation needs inventory | inventory current/history/local first |

## Stage 1.5 work order

Recommended implementation order:

1. Tax Debt final acceptance.
2. Tax Offences final acceptance.
3. FSSP inventory/recovery decision.
4. Fedresurs/EFРSB inventory/recovery decision.
5. Bankruptcy/liquidation event semantics on top of recovered/new evidence.
6. Arbitration Courts v1.
7. Courts of General Jurisdiction v1.
8. FNS bank-account suspension decisions.
9. Bank of Russia public high-risk/KYC technical probe and production-mode decision.
10. Free corporate-disclosure foundation.
11. Normalise company contacts/addresses/public bank details/mass address-director-founder facts needed by later Risk/Summary.
12. Full Stage 1.5 acceptance review.

Parallel work is allowed only when it does not risk database/schema conflicts or duplicate an existing source implementation.

## Permanent six-gate protocol

Every new/recovered source:

1. tests;
2. real public/official source result;
3. PostgreSQL write/readback;
4. correct `found / not_found / not_applicable / unavailable` semantics;
5. real Chromium/product verification;
6. record/date/coverage audit.

Code-ready, data-loaded, product-visible and auto-update-ready are separate statuses.

## Hard guardrails

- No Wave 2 during Stage 1.5.
- Free/public/official first.
- No paid source becomes a dependency without explicit product-owner decision.
- Do not duplicate FSSP/Fedresurs before inventory.
- Court claims are not confirmed debt.
- Liquidation is not bankruptcy.
- Source failure is never `not_found`.
- Risk is separate from `Полнота проверки`.
- Quantitative conclusions later must show actual numbers/period/calculation.
- Person/private data remains outside public company/SEO projection unless separately approved.

## Documents to read before Stage 1.5 coding

1. `WAVE1_CLOSURE_DECISION.md`
2. `WAVE1_STATUS.md`
3. `INTERMEDIATE_STAGE_1_5.md`
4. `WAVE_IMPLEMENTATION_PLAN.md`
5. `POST_WAVE1_PRODUCT_PLAN.md`
6. `SOURCE_ACCESS_COST_GATE.md`
7. `CORPORATE_DISCLOSURE_SOURCE_PLAN.md`
8. `COMPANY_CARD_V2_SCOPE.md`
9. `RISK_ENGINE_SPEC.md`
10. `SUMMARY_ENGINE_SPEC.md`
11. `COURT_INTELLIGENCE_SPEC.md`
12. `FINANCIAL_DISTRESS_SPEC.md`
13. `PROJECT_STATUS.md`

## Definition of Stage 1.5 completion

Stage 1.5 closes only when the criteria in `INTERMEDIATE_STAGE_1_5.md` are met.

After that the approved order is:

`Auto-update/Data Readiness -> Risk Engine -> Summary Engine -> Company Card v2 -> Report v1 -> Security Gate -> Legal Gate -> Product/Card Acceptance -> 10k SEO -> Lists/Bulk -> Monitoring -> Workspace -> Wave 2`.

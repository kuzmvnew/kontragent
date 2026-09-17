# WAVE IMPLEMENTATION PLAN — Kontragent

Status: ACTIVE
Date: 2026-09-17

## Wave 1 final state

**Wave 1: CLOSED / ACCEPTED BY EXPLICIT PROJECT DECISION.**

Authoritative closure decision: `WAVE1_CLOSURE_DECISION.md`.

- W1-001 CBR Warning List — ACCEPTED / SIX GATES PASS.
- W1-002 CBR FinOrg — ACCEPTED / SIX GATES PASS.
- W1-003 ФНС — МСП, получатели поддержки — ACCEPTED / SIX GATES PASS.
- W1-004 Росздравнадзор — ACCEPTED / SIX GATES PASS.
- W1-005 Роскомнадзор — DEFERRED EXCEPTION / SOURCE-BLOCKED (B, C). A/D/E/F implemented and verified; B/C remain `unavailable`; W1-005 itself is not relabeled SIX GATES PASS.
- W1-006 НОСТРОЙ / НОПРИЗ / СРО — ACCEPTED / SIX GATES PASS.

The unresolved W1-005 B/C external-source blocker has been explicitly dispositioned as a deferred exception and no longer blocks Wave 1 closure. It remains visible and may be revisited when the official source is complete/stable.

## Approved execution order

1. **Wave 1 — COMPLETE / CLOSED.**
2. **Intermediate Stage 1.5 — verify unfinished pre-Wave1 source acceptance and add/recover critical free sources required before Risk Engine.** See `INTERMEDIATE_STAGE_1_5.md`.
3. Build Auto-update / Data Readiness for production sources: schedules, last success, source_as_of, completeness, errors, retry/backoff and source status.
4. Build the full Risk Engine on top of the verified Stage 1.5 data foundation.
5. Build Summary Engine on top of verified facts/risk results.
6. Build Company Card v2 using `COMPANY_CARD_V2_SCOPE.md`.
7. Build Report v1.
8. Pass the mandatory Security & Resilience Gate.
9. Pass the mandatory Legal Launch Gate.
10. Pass the mandatory Product / Company Card Acceptance Gate: end-to-end user journey + source/data/risk/summary correctness.
11. Publish the first 10,000 high-quality company pages for SEO only after all three gates pass.
12. Build Lists + Bulk Check.
13. Build Monitoring / Event Engine + expandable notification types.
14. Build Workspace v1.
15. Only after the above Company B2B Core is ready, start Wave 2 source integrations through the Source Access & Cost Gate.
16. After Company B2B Core: Person Core.
17. Then separate Compliance module.
18. Then API / Enterprise / SSO / SLA / client integrations and government-procurement readiness when commercially justified.

## Current next stage

The project is now ready to start **Intermediate Stage 1.5**.

Stage 1.5 is not Wave 2. It exists so the Risk Engine is not built on an incomplete foundation.

Immediate Stage 1.5 scope:

- close final six-gate acceptance for FNS Tax Debt;
- close final six-gate acceptance for FNS Tax Offences;
- inventory/recover existing FSSP implementation before any rewrite;
- inventory/recover existing Fedresurs/EFРSB implementation before any rewrite;
- add/verify Courts v1: arbitration + courts of general jurisdiction;
- implement bankruptcy/liquidation event semantics instead of a single bankruptcy flag;
- add FNS account-suspension decisions;
- complete the Bank of Russia KYC/high-risk public-check technical probe;
- add free-first corporate disclosure foundation;
- normalise Company Card facts needed later by Risk/Summary.

Detailed acceptance criteria: `INTERMEDIATE_STAGE_1_5.md`.

## Mandatory post-Wave1 specifications

- `INTERMEDIATE_STAGE_1_5.md`
- `POST_WAVE1_PRODUCT_PLAN.md`
- `PRODUCT_FUNCTION_CATALOG.md`
- `COMPANY_CARD_V2_SCOPE.md`
- `RISK_ENGINE_SPEC.md`
- `SUMMARY_ENGINE_SPEC.md`
- `FINANCIAL_DISTRESS_SPEC.md`
- `COURT_INTELLIGENCE_SPEC.md`
- `EVENT_NOTIFICATION_MODEL.md`
- `CORPORATE_DISCLOSURE_SOURCE_PLAN.md`
- `TRANSPORT_SOURCE_AUDIT.md`
- `SOURCE_ACCESS_COST_GATE.md`
- `SECURITY_RESILIENCE_GATE.md`
- `LEGAL_LAUNCH_GATE.md`
- `PRODUCT_CARD_ACCEPTANCE_GATE.md`

## Guardrails

- Do not start Wave 2 during Stage 1.5.
- Free/public/official sources are preferred; a paid source requires a separate explicit product decision.
- Before reimplementing FSSP/Fedresurs, inventory current/historical/local work and reuse verified code/data where possible.
- Every new/recovered source must close a concrete product workflow and retain source/date/evidence.
- New sources extend Evidence/Facts/Derived Metrics/Events/rules; they must not force a rewrite of Risk, Summary, Monitoring or Bulk engines.
- Risk and `Полнота проверки` remain separate.
- `found`, `not_found`, `not_applicable` and `unavailable` remain distinct states.
- Quantitative conclusions show the actual numbers, period and calculation when applicable.
- Court claims are not confirmed debt.
- Liquidation is not automatically bankruptcy.
- Forecasts are not facts.
- Public SEO projection includes only legally approved fields/conclusions.
- Private person evidence remains isolated from public company/API/SEO projections unless separately approved.

## Source backlog

The researched source backlog remains indexed by `MASTER_SOURCE_MATRIX.md` and is expanded by source-specific research documents such as `TRANSPORT_SOURCE_AUDIT.md`.

Stage 1.5 critical sources are not treated as Wave 2 merely because they are added after Wave 1; they are prerequisite foundation for the agreed Risk/Summary/Card product path.

Before implementation of later Wave 2 sources, apply `SOURCE_ACCESS_COST_GATE.md`.

W1-005 B/C stays in the deferred-source backlog as an external blocker, not as an unfinished Wave 1 gate.

Project name remains **Kontragent**. Names using “Next” are working product/module naming ideas only and are not the approved project name.

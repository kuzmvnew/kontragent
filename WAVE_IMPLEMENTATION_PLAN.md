# WAVE IMPLEMENTATION PLAN — Kontragent

Status: ACTIVE — PRODUCT RECOVERY V3 IN PROGRESS; COMPANY CARD V2 BLOCKED
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
2. **Intermediate Stage 1.5 — COMPLETE / CLOSED / ACCEPTED.** See `STAGE_1_5_ACCEPTANCE.md`.
3. **Auto-update / Data Readiness — COMPLETE / ACCEPTED.** Operational registry, freshness, run history, atomic bulk contract, locks/backoff and internal status panel were accepted and merged in PR #40. Production scheduler deployment remains honestly `NOT_CONFIGURED` until handlers/supervisor are installed.
4. **Risk Engine — COMPLETE / ACCEPTED.** Explainable signals, applicability, immutable assessments and separate completeness accepted in merged PR #43.
5. **Summary Engine — COMPLETE / ACCEPTED.** Deterministic traceable summaries, modes, persistence, cache, explainability and minimal acceptance UI accepted in PR #44; merge remains a separate owner command.
6. **Current stage: Product Recovery v3 — IN PROGRESS.** Operationalize the mandatory runtime sources, rerun the same 40-company regression, raise Coverage and pass the positive gate.
7. **Company Card v2 — BLOCKED** until Product Recovery acceptance; then use `COMPANY_CARD_V2_SCOPE.md`.
8. Build Report v1.
9. Pass the mandatory Security & Resilience Gate.
10. Pass the mandatory Legal Launch Gate.
11. Pass the mandatory Product / Company Card Acceptance Gate: end-to-end user journey + source/data/risk/summary correctness.
12. Publish the first 10,000 high-quality company pages for SEO only after all three gates pass.
13. Build Lists + Bulk Check.
14. Build Monitoring / Event Engine + expandable notification types.
15. Build Workspace v1.
16. Only after the above Company B2B Core is ready, start Wave 2 source integrations through the Source Access & Cost Gate.
17. After Company B2B Core: Person Core.
18. Then separate Compliance module.
19. Then API / Enterprise / SSO / SLA / client integrations and government-procurement readiness when commercially justified.

## Current next stage

Wave 1 is **CLOSED / ACCEPTED**. Stage 1.5 is **CLOSED / ACCEPTED**. Auto-update / Data Readiness, Risk Engine v1 and Summary Engine v1 are **COMPLETE / ACCEPTED** historical stages. The current stage is **PRODUCT RECOVERY V3 — IN PROGRESS**. Company Card v2 is **BLOCKED** until Product Recovery acceptance.

Current Product Recovery evidence: 40/40 unique companies processed; Coverage
47–80/100, average 58/100; positive gate 0/40. Remaining engineering work is to
operationalize FSSP, EFRSB, CBR ZSK and Bankinform, fill Arbitration 40/40,
improve General Courts coverage and rerun the same regression. Existing runner
code is not treated as completed runtime coverage.

Stage 1.5 and Data Readiness were not Wave 2. Their accepted foundations prevent the Risk Engine from being built on an incomplete, stale or semantically unsafe base.

Accepted Stage 1.5 scope (historical boundary; all items are dispositioned in `STAGE_1_5_ACCEPTANCE.md`):

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

- Do not start Wave 2 before the approved Company B2B Core sequence is complete.
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

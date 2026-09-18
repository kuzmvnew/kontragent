# WAVE IMPLEMENTATION PLAN — Kontragent

Status: ACTIVE — STAGE 1.6 AUDIT CLOSED; PRODUCT RECOVERY V3 EXTERNALLY BLOCKED; COMPANY CARD V2 BLOCKED
Date: 2026-09-18

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
6. **Product Recovery v3 — EXTERNALLY BLOCKED / NOT ACCEPTED.**
7. **Stage 1.6 audit — CLOSED / PRODUCT RECOVERY NOT ACCEPTED.** Runtime hardening and audit closure are verified; external Source Runtime Completion and Golden-40 positive acceptance remain open. See `STAGE_1_6_AUDIT_CLOSURE.md`.
8. Pass Golden-40 Acceptance on the same cohort without weakening product rules.
9. Record **Product Recovery ACCEPTED** only after that gate passes.
10. Create and merge the separate Recovery PR to `main` only on explicit owner instruction.
11. Build **Company Card v2** using `COMPANY_CARD_V2_SCOPE.md`.
12. Build Report v1.
13. Build **Platform 2.0**: Source SDK; canonical Evidence/Facts/Derived Metrics; versioned Company Projection; dependency graph and incremental recalculation; Enrichment Orchestrator; durable idempotent jobs/workers; anomaly detection/quarantine; production scheduler; observability; backup/restore; load infrastructure.
14. Enrich the first 10,000 companies.
15. Pass the Data Quality Gate.
16. Produce the Production Candidate.
17. Pass the mandatory Security & Resilience Gate v2.
18. Pass the mandatory Legal Launch Gate.
19. Pass the mandatory Product / Company Card Acceptance Gate: end-to-end user journey + source/data/risk/summary correctness.
20. Publish the first 10,000 high-quality company pages for SEO only after all gates pass.
21. Build Lists + Bulk Check.
22. Build Monitoring / Event Engine + expandable notification types.
23. Build Workspace v1.
24. Only after the above Company B2B Core is ready, start Wave 2 source integrations through the Source Access & Cost Gate.
25. After Company B2B Core: Person Core.
26. Then separate Compliance module.
27. Then API / Enterprise / SSO / SLA / client integrations and government-procurement readiness when commercially justified.

## Current next stage

Wave 1 is **CLOSED / ACCEPTED**. Stage 1.5 is **CLOSED / ACCEPTED**. Auto-update / Data Readiness, Risk Engine v1 and Summary Engine v1 are **COMPLETE / ACCEPTED** historical stages. Stage 1.6 audit is **CLOSED / PRODUCT RECOVERY NOT ACCEPTED**. Product Recovery v3 is externally blocked, and Company Card v2 and all later stages remain **BLOCKED** until Product Recovery acceptance and their ordered prerequisites.

Current Product Recovery evidence: 40/40 unique companies processed; Workflow
Completion 100%; Evidence Coverage 53–88/100, average 65.3/100; positive gate
0/40 overall and 0/10 in the low-risk candidate bucket. Remaining runtime gaps
are Arbitration 39/40, General Courts 21/40, CBR ZSK 40/40, FSSP 24/40,
EFRSB 21/40 and applicable licences/SRO 1/40. Bankinform is contextual N/A,
RNP is separately deferred, and ERKNM is 40/40 terminal. The documented EFRSB
REST client is implemented but production credentials are absent. Existing code
without completed evidence is not treated as runtime coverage. Exact reasons and
close conditions are recorded in `STAGE_1_6_AUDIT_CLOSURE.md`.

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

- `STAGE_1_6_AUDIT_CLOSURE.md`
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
- Public MVP and Commercial B2B MVP are separate milestones. Public MVP includes Search, Card, Risk, Summary, Report, Platform 2.0, 10k enrichment, Security, Legal, Product Acceptance and SEO. Commercial B2B MVP adds account/workspace, saved lists, bulk checks, monitoring, notifications, export and minimal tariff/usage controls.
- Platform 2.0 non-goals are Kubernetes, Kafka, OpenSearch and RabbitMQ unless Stage 1.6 objectively requires them, microservices and a full async rewrite.

## Source backlog

The researched source backlog remains indexed by `MASTER_SOURCE_MATRIX.md` and is expanded by source-specific research documents such as `TRANSPORT_SOURCE_AUDIT.md`.

Stage 1.5 critical sources are not treated as Wave 2 merely because they are added after Wave 1; they are prerequisite foundation for the agreed Risk/Summary/Card product path.

Before implementation of later Wave 2 sources, apply `SOURCE_ACCESS_COST_GATE.md`.

W1-005 B/C stays in the deferred-source backlog as an external blocker, not as an unfinished Wave 1 gate.

Project name remains **Kontragent**. Names using “Next” are working product/module naming ideas only and are not the approved project name.

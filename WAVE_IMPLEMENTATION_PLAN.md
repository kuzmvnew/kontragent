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
2. Freeze the approved post-Wave1 product/function specifications and keep them synchronized with actual code.
3. Build Auto-update / Data Readiness for production sources: schedules, last success, source_as_of, coverage, errors, retry/backoff and source status.
4. Build Company Card v2 + Risk Engine + Summary Engine + Report v1.
5. Pass the mandatory Security & Resilience Gate.
6. Pass the mandatory Legal Launch Gate.
7. Pass the mandatory Product / Company Card Acceptance Gate: end-to-end user journey + source/data/risk/summary correctness.
8. Publish the first 10,000 high-quality company pages for SEO only after all three gates pass.
9. Build Lists + Bulk Check.
10. Build Monitoring / Event Engine + expandable notification types.
11. Build Workspace v1.
12. Only after the above Company B2B Core is ready, start Wave 2 source integrations through the Source Access & Cost Gate.
13. After Company B2B Core: Person Core.
14. Then separate Compliance module.
15. Then API / Enterprise / SSO / SLA / client integrations and government-procurement readiness when commercially justified.

## Current next stage

The project is now in the post-Wave1 transition. Do not start Wave 2 sources yet.

Immediate sequence:

1. synchronize/freeze the post-Wave1 specifications with the accepted code baseline;
2. execute Auto-update / Data Readiness;
3. execute Company Card v2 + Risk/Summary/Report;
4. pass the three mandatory launch/product gates before 10k SEO.

Returning to W1-005 B/C is a separate deferred-source maintenance task and does not automatically reopen Wave 1.

## Mandatory post-Wave1 specifications

- `POST_WAVE1_PRODUCT_PLAN.md`
- `PRODUCT_FUNCTION_CATALOG.md`
- `RISK_ENGINE_SPEC.md`
- `SUMMARY_ENGINE_SPEC.md`
- `FINANCIAL_DISTRESS_SPEC.md`
- `COURT_INTELLIGENCE_SPEC.md`
- `EVENT_NOTIFICATION_MODEL.md`
- `TRANSPORT_SOURCE_AUDIT.md`
- `SOURCE_ACCESS_COST_GATE.md`
- `SECURITY_RESILIENCE_GATE.md`
- `LEGAL_LAUNCH_GATE.md`
- `PRODUCT_CARD_ACCEPTANCE_GATE.md`

## Guardrails

- Do not start Wave 2 merely because another registry exists.
- Every new source must close a concrete product workflow and pass access/cost/legal review first.
- New sources extend Evidence/Facts/Derived Metrics/Events/rules; they must not force a rewrite of Risk, Summary, Monitoring or Bulk engines.
- Risk and Coverage remain separate.
- `found`, `not_found`, `not_applicable` and `unavailable` remain distinct states.
- Quantitative conclusions show the actual numbers, period and calculation when applicable.
- Court claims are not confirmed debt.
- Forecasts are not facts.
- Public SEO projection includes only legally approved fields/conclusions.
- Private person evidence remains isolated from public company/API/SEO projections unless separately approved.

## Source backlog

The researched source backlog remains indexed by `MASTER_SOURCE_MATRIX.md` and is expanded by source-specific research documents such as `TRANSPORT_SOURCE_AUDIT.md`. Before implementation, every new Wave 2 source must pass `SOURCE_ACCESS_COST_GATE.md`.

W1-005 B/C stays in the deferred-source backlog as an external blocker, not as an unfinished Wave 1 gate.

Project name remains **Kontragent**. Names using “Next” are working product/module naming ideas only and are not the approved project name.

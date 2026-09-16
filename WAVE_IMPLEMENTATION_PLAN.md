# WAVE IMPLEMENTATION PLAN — Kontragent

Status: ACTIVE
Date: 2026-09-17

## Current Wave 1 state

- W1-001 CBR Warning List — ACCEPTED / SIX GATES PASS.
- W1-002 CBR FinOrg — ACCEPTED / SIX GATES PASS.
- W1-003 ФНС — МСП, получатели поддержки — ACCEPTED / SIX GATES PASS.
- W1-004 Росздравнадзор — ACCEPTED / SIX GATES PASS.
- W1-005 Роскомнадзор — IN PROGRESS / SOURCE-BLOCKED (B, C). A/D/E/F implemented and verified; the source remains open and is not accepted by all six gates.
- W1-006 НОСТРОЙ / СРО — NEXT EXECUTABLE / NOT STARTED.

W1-006 may proceed while W1-005 waits for complete official B/C source responses. This does not close W1-005. Wave 1 remains open until W1-006 is accepted and the W1-005 blocker is resolved or separately dispositioned by an explicit project decision.

## Approved execution order

1. Finish Wave 1 under the status rule above.
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

## Source backlog

The researched source backlog remains indexed by `MASTER_SOURCE_MATRIX.md` and is expanded by source-specific research documents such as `TRANSPORT_SOURCE_AUDIT.md`. Before implementation, every new Wave 2 source must pass `SOURCE_ACCESS_COST_GATE.md`.

Project name remains **Kontragent**. Names using “Next” are working product/module naming ideas only and are not the approved project name.

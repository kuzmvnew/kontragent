# Roadmap Changes After Audit

Version: 2.0

Status: APPROVED

Date: 2026-09-17

---

## Purpose

This document records approved architectural and product clarifications introduced after the Architecture, Business & Scale, Founder and procurement-TZ audits. It supplements `ROADMAP.md` without rewriting the project foundation.

---

## Main Decision

The project architecture is NOT rewritten.

The existing foundation remains:

- FastAPI;
- PostgreSQL;
- Master Registry;
- Contracts;
- Evidence;
- Aggregators;
- Ingestion;
- Dataset Registry.

The architecture evolves by adding stable product engines and source adapters on top of that foundation.

---

## Approved Product Evolution

`Official Sources`

↓

`Normalization`

↓

`Evidence / Facts`

↓

`Applicability / Derived Metrics`

↓

`Risk Rules / Section Assessments`

↓

`Summary / Recommendations`

↓

`Company Card / Report / Bulk / Monitoring`

↓

`User`

AI wording may assist presentation later, but facts and material conclusions remain traceable to structured Evidence, calculations and rules.

---

## Approved Components

The approved architecture includes:

- Entity Registry;
- Relationship Registry;
- Facts Layer;
- Dataset Quality / Coverage;
- Check / Risk Engine;
- Summary Engine;
- Financial Distress Forecast as a separate future analytical layer;
- Court Intelligence as a staged capability;
- Event Engine;
- Notification Engine;
- Lists / Bulk Check;
- Workspace v1;
- later Person Core;
- later Compliance;
- later API / Enterprise.

Detailed specifications:

- `POST_WAVE1_PRODUCT_PLAN.md`
- `PRODUCT_FUNCTION_CATALOG.md`
- `RISK_ENGINE_SPEC.md`
- `SUMMARY_ENGINE_SPEC.md`
- `FINANCIAL_DISTRESS_SPEC.md`
- `COURT_INTELLIGENCE_SPEC.md`
- `EVENT_NOTIFICATION_MODEL.md`
- `TRANSPORT_SOURCE_AUDIT.md`
- `SOURCE_ACCESS_COST_GATE.md`

---

## Approved execution order

1. Complete Wave 1.
2. Auto-update / Data Readiness.
3. Company Card v2 + Risk Engine + Summary Engine + Report v1.
4. Security & Resilience Gate.
5. Legal Launch Gate.
6. Product / Company Card Acceptance Gate.
7. First 10,000 high-quality SEO company cards.
8. Lists + Bulk Check.
9. Monitoring / Event Engine + expandable notification types.
10. Workspace v1.
11. Wave 2 source integrations through Source Access & Cost Gate.
12. Person Core.
13. Compliance.
14. API / Enterprise / SSO / SLA / internal integrations when commercially justified.

This order supersedes the older simplified sequence “Company -> SEO -> Monitoring -> Person ...” where it conflicts with the approved pre-SEO gates and Company B2B Core work above.

---

## Three mandatory gates before 10k SEO

Mass SEO publication requires all three:

1. `SECURITY_RESILIENCE_GATE.md`
2. `LEGAL_LAUNCH_GATE.md`
3. `PRODUCT_CARD_ACCEPTANCE_GATE.md`

The third gate is not only a UI smoke test. It verifies the complete chain:

`official source -> DB -> Evidence -> Fact -> Derived Metric -> Risk Rule -> Signal -> Summary -> card/report`

and the real user journey from entry/search/clicks to registration/authentication and access states.

---

## Risk / Coverage clarification

Risk and Coverage are independent.

- a confirmed risk is not reduced because another source is unavailable;
- `not_found`, `not_applicable` and `unavailable` are different states;
- a rule is not evaluated if its required input is missing;
- context-specific checks are only applied where relevant;
- quantitative conclusions show actual values, period and calculation;
- court claims are not confirmed debt;
- forecasts are not facts.

---

## Bankruptcy / financial distress clarification

Official bankruptcy procedure is a factual legal event and does not require a score formula.

Financial-distress/bankruptcy forecast is a separate analytical model based on verified financial and risk features, with GIR BO as a future important input where access/cost is justified. The model is versioned and later calibrated on historical Russian-company outcomes.

Operational OFD/online-cash-register data may be used only under a lawful own-company/client access scenario; absence of such private data is not a negative signal.

---

## Court Intelligence clarification

Court work is staged:

1. case facts, roles, counts, amounts, dynamics and materiality relative to business scale;
2. analysis inside cases: instances, documents, claims, arguments, evidence, legal rules, outcomes;
3. future probabilistic outcome modelling after enough lawful historical data exists.

Predicted court outcome remains separate from actual court facts.

---

## Source strategy clarification

Do not plan Wave 2 as a raw list of registries. Every new source must:

- close a concrete workflow;
- pass access/legal/cost review;
- define matching and coverage;
- publish Facts/Events into existing engines instead of forcing a redesign.

Transport backlog is expanded in `TRANSPORT_SOURCE_AUDIT.md`.

Before reimplementing FSSP or Fedresurs, inventory current `main`, historical branches and the earlier/local project state because the user has indicated those sources may already exist outside the currently confirmed main implementation.

---

## Founder Rules

1. No approved release may be delayed by unrelated feature creep.
2. New ideas go to a controlled backlog/specification.
3. Release before unnecessary completeness, but never bypass mandatory security, legal or product-correctness gates.
4. Do not duplicate completed work.
5. Major conclusions remain explainable and source-backed.

---

## Current Wave 1 execution clarification — 2026-09-17

Actual `main` head checked before this documentation update: `911bc8d211beb68deccf19b8df54d197ccd6aeda`.

- W1-001 — ACCEPTED / SIX GATES PASS.
- W1-002 — ACCEPTED / SIX GATES PASS.
- W1-003 — ACCEPTED / SIX GATES PASS.
- W1-004 — ACCEPTED / SIX GATES PASS.
- W1-005 Roskomnadzor — IN PROGRESS / SOURCE-BLOCKED (B, C). A/D/E/F implemented and verified; B/C remain unavailable because official source responses terminate before complete XML EOF. W1-005 is not accepted.
- W1-006 NOSTROY / SRO — NEXT EXECUTABLE / NOT STARTED.

W1-006 may start while W1-005 waits on the external source blocker. This does not close W1-005 or Wave 1.

The six permanent source acceptance criteria remain: tests -> real official response -> PostgreSQL write/readback -> correct found/not_found/not_applicable/unavailable semantics -> real browser card -> record/date/coverage verification.

---

## Status

APPROVED.

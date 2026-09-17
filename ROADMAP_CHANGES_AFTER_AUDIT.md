# Roadmap Changes After Audit

Version: 3.0

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

`Official/Public Sources`

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
- Dataset Quality / `Полнота проверки`;
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

- `INTERMEDIATE_STAGE_1_5.md`
- `STAGE_1_5_HANDOFF.md`
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

---

## Wave 1 final state

Wave 1 is officially **CLOSED / ACCEPTED BY EXPLICIT PROJECT DECISION**.

See `WAVE1_CLOSURE_DECISION.md` and `WAVE1_STATUS.md`.

Preserved factual status:

- W1-001 — ACCEPTED / SIX GATES PASS;
- W1-002 — ACCEPTED / SIX GATES PASS;
- W1-003 — ACCEPTED / SIX GATES PASS;
- W1-004 — ACCEPTED / SIX GATES PASS;
- W1-005 Roskomnadzor — DEFERRED EXCEPTION / SOURCE-BLOCKED (B, C); A/D/E/F verified; B/C remain `unavailable`;
- W1-006 NOSTROY / NOPRIZ / SRO — ACCEPTED / SIX GATES PASS.

W1-005 B/C is not relabelled as success and does not become `not_found`.

---

## Approved execution order

1. Wave 1 — CLOSED.
2. **Intermediate Stage 1.5** — unfinished old-source acceptance + critical free-source foundation before Risk Engine.
3. Auto-update / Data Readiness.
4. Risk Engine.
5. Summary Engine.
6. Company Card v2.
7. Report v1.
8. Security & Resilience Gate.
9. Legal Launch Gate.
10. Product / Company Card Acceptance Gate.
11. First 10,000 high-quality SEO company cards.
12. Lists + Bulk Check.
13. Monitoring / Event Engine + expandable notification types.
14. Workspace v1.
15. Wave 2 source integrations through Source Access & Cost Gate.
16. Person Core.
17. Compliance.
18. API / Enterprise / SSO / SLA / internal integrations when commercially justified.

This order supersedes older simplified sequences where they conflict with Stage 1.5 or the approved pre-SEO gates.

---

## Intermediate Stage 1.5 clarification

Stage 1.5 is not Wave 2.

It is required because the full Risk Engine should not be built before these critical checks are either accepted, recovered or explicitly classified unavailable:

- FNS Tax Debt final acceptance;
- FNS Tax Offences final acceptance;
- FSSP inventory/recovery;
- Fedresurs/EFРSB inventory/recovery;
- bankruptcy/liquidation event semantics;
- arbitration courts v1;
- courts of general jurisdiction v1;
- FNS bank-account suspension decisions;
- Bank of Russia public high-risk/KYC technical probe;
- free corporate disclosure;
- company contact/address/public-bank/mass-address/director/founder facts required by later Risk/Summary.

Detailed scope and Definition of Done: `INTERMEDIATE_STAGE_1_5.md`.

---

## Three mandatory gates before 10k SEO

Mass SEO publication requires all three:

1. `SECURITY_RESILIENCE_GATE.md`
2. `LEGAL_LAUNCH_GATE.md`
3. `PRODUCT_CARD_ACCEPTANCE_GATE.md`

The third gate is not only a UI smoke test. It verifies the complete chain:

`source -> DB -> Evidence -> Fact -> Derived Metric -> Risk Rule -> Signal -> Summary -> card/report`

and the real user journey from entry/search/clicks to registration/authentication and access states.

---

## Risk / completeness clarification

Risk and `Полнота проверки` are independent.

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

Bankruptcy must be represented as events/stages where evidence supports them, including intent, application, court acceptance, introduced procedures and completion/termination.

Liquidation is a separate legal process and must not automatically be labelled bankruptcy.

Financial-distress/bankruptcy forecast is a separate analytical model based on verified financial and risk features, with GIR BO as a future input only when access/cost is justified or an equivalent lawful free data path is available.

Operational OFD/online-cash-register data may be used only under a lawful own-company/client access scenario; absence of such private data is not a negative signal.

---

## Court Intelligence clarification

Court work is staged:

1. case facts, roles, counts, amounts, dynamics and materiality relative to business scale;
2. analysis inside cases: instances, documents, claims, arguments, evidence, legal rules, outcomes;
3. future probabilistic outcome modelling after enough lawful historical data exists.

Predicted court outcome remains separate from actual court facts.

Court v1 now belongs to Stage 1.5 because the first full Risk Engine should not be designed without court facts.

---

## Source strategy clarification

Free/public/official first.

Do not plan Wave 2 as a raw list of registries. Every new source must:

- close a concrete workflow;
- pass access/legal/cost review;
- define matching and completeness;
- publish Facts/Events into existing engines instead of forcing a redesign.

A paid source requires a separate explicit product decision after free alternatives are checked.

Corporate disclosure is a Stage 1.5 free-first source family. See `CORPORATE_DISCLOSURE_SOURCE_PLAN.md`.

Transport backlog is expanded in `TRANSPORT_SOURCE_AUDIT.md` and remains Wave 2 unless a separate product decision moves a specific registry earlier.

Before reimplementing FSSP or Fedresurs, inventory current `main`, historical branches and the earlier/local project state because the product owner states those sources were developed previously.

---

## Founder Rules

1. No approved release may be delayed by unrelated feature creep.
2. New ideas go to a controlled backlog/specification.
3. Release before unnecessary completeness, but never bypass mandatory data-quality, security, legal or product-correctness gates.
4. Do not duplicate completed work.
5. Major conclusions remain explainable and source-backed.
6. Do not buy paid data merely for convenience when a practical free/public source can close the same required workflow.

---

## Historical status checkpoint — 2026-09-17 (superseded)

Wave 1 is closed.

At the time of this checkpoint, the next executable stage was:

**Intermediate Stage 1.5 — READY TO START.**

This checkpoint is retained as history. Stage 1.5 and Auto-update / Data Readiness were subsequently accepted. Current status and order are authoritative in `PROJECT_STATUS.md` and `WAVE_IMPLEMENTATION_PLAN.md`; the current approved next stage is Risk Engine, not started by this documentation cleanup.

Development handoff: `STAGE_1_5_HANDOFF.md`.

The six permanent source acceptance criteria remain: tests -> real official/public response -> PostgreSQL write/readback -> correct found/not_found/not_applicable/unavailable semantics -> real browser card -> record/date/completeness verification.

---

## Status

APPROVED.

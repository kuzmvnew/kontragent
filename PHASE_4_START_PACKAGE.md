# PHASE 4 START PACKAGE

Version: 1.2

Status: APPROVED

Date: 2026-09-17

---

# Phase

Phase 4

Data Normalization & Quality Foundation

---

# Mission

Build a unified business data foundation.

Wave 1 is now **CLOSED / ACCEPTED BY EXPLICIT PROJECT DECISION**. Five source steps are fully accepted by Six Gates. W1-005 Roskomnadzor remains a factual `DEFERRED EXCEPTION / SOURCE-BLOCKED (B, C)`: A/D/E/F are implemented and verified, while B/C remain `unavailable` because the official responses terminate before complete XML EOF. W1-005 is not relabeled SIX GATES PASS.

W1-006 NOSTROY / NOPRIZ / SRO is `ACCEPTED / SIX GATES PASS`.

The W1-005 external blocker no longer blocks Wave 1 closure and stays in deferred-source maintenance/backlog. See `WAVE1_CLOSURE_DECISION.md`.

Current official sources become one coherent business model.

---

# Main Objectives

1. Entity Registry

Create unified business entities.

Initial entities:

- Company
- Sole Proprietor
- Person
- Court Case
- Enforcement
- License
- Trademark
- Patent

2. Relationship Registry

Supported relationships:

- Person → Company
- Company → Company
- Company → Court Case
- Company → Enforcement
- Company → License
- Person → Trademark

3. Facts Layer

Every fact must contain:

- entity
- source
- value
- evidence
- date
- quality

4. Dataset Quality

Every dataset must expose:

- version
- update date
- record count
- quality
- status

5. Evidence Layer

Every business conclusion must reference evidence.

---

# Out of Scope during the completed Wave 1 source work

The following product tracks were not implemented as part of W1-006 itself:

- Person Product
- Leads
- CRM
- Enterprise
- Monitoring
- SEO release
- AI Chat

Their approved later order is documented in `POST_WAVE1_PRODUCT_PLAN.md` and `WAVE_IMPLEMENTATION_PLAN.md`.

---

# Deliverables

Mandatory:

- Entity Registry
- Relationship Registry
- Facts Layer
- Dataset Quality
- Evidence Layer
- Tests
- Updated Documentation

---

# Acceptance Criteria

Phase 4 source work is accepted only when:

- Every supported dataset maps to business entities where matching is justified.
- Every fact references an official source.
- Every fact has evidence.
- Relationships are normalized.
- Dataset quality information is available.
- Tests pass.
- Each source has separate evidence for all six permanent source criteria: real official response, PostgreSQL write/read, correct state semantics, real browser card, dates/counts/coverage in addition to tests.

Code, downloaded data, database import, browser rendering, measured coverage and configured auto-update are different statuses.

Wave-level governance may explicitly disposition an external source blocker without rewriting that source's own factual acceptance status. `WAVE1_CLOSURE_DECISION.md` records the W1-005 B/C exception.

---

# Wave 1 Final Handoff

- W1-001 CBR Warning List: ACCEPTED / SIX GATES PASS on user Mac.
- W1-002 CBR FinOrg: ACCEPTED / SIX GATES PASS on user Mac; protocol `docs/W1_002_ACCEPTANCE.md`.
- W1-003 FNS SME support recipients: ACCEPTED / SIX GATES PASS on user Mac; protocol `docs/W1_003_ACCEPTANCE.md`.
- W1-004 Roszdravnadzor: ACCEPTED / SIX GATES PASS; protocol `docs/W1_004_ACCEPTANCE.md`.
- W1-005 Roskomnadzor: `DEFERRED EXCEPTION / SOURCE-BLOCKED (B, C)`; A/D/E/F implemented and verified; B/C remain `unavailable`; Six Gates for W1-005 itself NOT CONFIRMED.
- W1-006 NOSTROY / NOPRIZ / SRO: `ACCEPTED / SIX GATES PASS`; protocol `docs/W1_006_ACCEPTANCE.md`; private person data remains isolated and non-public.
- Wave 1 overall: **CLOSED / ACCEPTED BY EXPLICIT PROJECT DECISION**.
- RNP/EIS: deferred until official access.
- DaMIA: not connected.

---

# Immediate Next Step

Do not start a new Wave source.

Proceed with the approved post-Wave1 transition:

1. freeze/synchronize post-Wave1 specifications with the accepted implementation baseline;
2. Auto-update / Data Readiness;
3. Company Card v2 + Risk Engine + Summary Engine + Report v1;
4. mandatory Security & Resilience, Legal Launch and Product / Company Card Acceptance gates;
5. first 10k SEO only after those gates.

W1-005 B/C may be revisited later as deferred source maintenance when the official source becomes complete/stable. This does not automatically reopen Wave 1.

---

# Success Definition

The existing official datasets become one unified business model capable of supporting:

- Checks
- Monitoring
- Executive Summary
- AI Explain

without redesign of the data foundation.

The post-Wave1 product engines are specified separately so new sources can plug into Evidence/Facts/Derived Metrics/Events without rewriting Risk/Summary/Monitoring.

---

# Next Product Work After Wave 1

Follow `POST_WAVE1_PRODUCT_PLAN.md` and `WAVE_IMPLEMENTATION_PLAN.md`.

Before 10k SEO, all three mandatory gates must pass:

- `SECURITY_RESILIENCE_GATE.md`
- `LEGAL_LAUNCH_GATE.md`
- `PRODUCT_CARD_ACCEPTANCE_GATE.md`

---

Status

APPROVED

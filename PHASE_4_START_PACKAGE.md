# PHASE 4 START PACKAGE

Version: 1.1

Status: APPROVED

Date: 2026-09-17

---

# Phase

Phase 4

Data Normalization & Quality Foundation

---

# Mission

Build a unified business data foundation.

Approved clarification: all six agreed Wave 1 source steps remain part of Wave 1. Five are fully accepted. W1-005 Roskomnadzor is implemented for A–F but remains `IN PROGRESS / SOURCE-BLOCKED (B, C)` because the official B/C bulk responses terminate before complete XML EOF. W1-006 NOSTROY / NOPRIZ / SRO is `ACCEPTED / SIX GATES PASS`.

W1-006 may proceed while the external W1-005 blocker remains open. This does not mark W1-005 or Wave 1 complete.

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

# Out of Scope during Wave 1 / current Phase 4 source work

The following product tracks are not implemented as part of W1-006 itself:

- Person Product
- Leads
- CRM
- Enterprise
- Monitoring
- SEO release
- AI Chat

Their approved later order is documented in `POST_WAVE1_PRODUCT_PLAN.md`.

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

---

# Wave 1 Handoff

- W1-001 CBR Warning List: ACCEPTED / SIX GATES PASS on user Mac.
- W1-002 CBR FinOrg: ACCEPTED / SIX GATES PASS on user Mac; protocol `docs/W1_002_ACCEPTANCE.md`.
- W1-003 FNS SME support recipients: ACCEPTED / SIX GATES PASS on user Mac; protocol `docs/W1_003_ACCEPTANCE.md`.
- W1-004 Roszdravnadzor: ACCEPTED / SIX GATES PASS; protocol `docs/W1_004_ACCEPTANCE.md`.
- W1-005 Roskomnadzor: `IN PROGRESS / SOURCE-BLOCKED (B, C)`; A/D/E/F implemented and verified; B/C remain `unavailable`; 458 tests PASS; Six Gates NOT CONFIRMED.
- W1-006 NOSTROY / NOPRIZ / SRO: `ACCEPTED / SIX GATES PASS`; protocol `docs/W1_006_ACCEPTANCE.md`; private person data remains isolated and non-public.
- RNP/EIS: deferred until official access.
- DaMIA: not connected.
- Person / Leads / CRM / Enterprise: do not start during Wave 1.

---

# Immediate Next Step

Do not start a new Wave source. Preserve the accepted W1-006 evidence and wait
for a separate decision or corrected official responses for W1-005 B/C. Do not
treat W1-006 acceptance as permission to mark Wave 1 complete.

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

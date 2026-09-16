# PHASE 4 START PACKAGE

Version: 1.0

Status: APPROVED

Date: 2026-09-16

---

# Phase

Phase 4

Data Normalization & Quality Foundation

---

# Mission

Build a unified business data foundation.

Approved clarification (2026-09-16): all six agreed Wave 1 sources must be accepted before SEO MVP; they are not all complete yet. Wave 1 содержит шесть источников. W1-001 и W1-003 приняты на Mac. W1-002: LIVE + CACHE PASS на Mac, полный реальный CI PASS; локальная browser/six-gate приёмка ожидается. W1-004 Росздравнадзор, W1-005 Роскомнадзор, W1-006 НОСТРОЙ / СРО — NOT STARTED. Точка входа: WAVE1_STATUS.md. Порядок фаз и архитектура не меняются.

Current official sources become one coherent business model.

---

# Main Objectives

1.

Entity Registry

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

---

2.

Relationship Registry

Supported relationships:

- Person → Company
- Company → Company
- Company → Court Case
- Company → Enforcement
- Company → License
- Person → Trademark

---

3.

Facts Layer

Every fact must contain:

- entity
- source
- value
- evidence
- date
- quality

---

4.

Dataset Quality

Every dataset must expose:

- version
- update date
- record count
- quality
- status

---

5.

Evidence Layer

Every business conclusion must reference evidence.

---

# Out of Scope

The following items are NOT implemented during Phase 4.

- Person Product
- Leads
- CRM
- Enterprise
- Monitoring
- SEO improvements
- AI Chat

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

Phase 4 is complete when:

- Every supported dataset maps to business entities.
- Every fact references an official source.
- Every fact has evidence.
- Relationships are normalized.
- Dataset quality information is available.
- Tests pass.
- Each source has separate evidence for all six acceptance criteria in PROJECT_STATUS.md, including a real browser, PostgreSQL readback and dated coverage. CI and the user's database are reported separately.

W1-001 local acceptance evidence on 16.09.2026: 27,163 official source/imported/saved records; 2,995 with usable INN; 24,168 without usable INN; 857 exact-INN links to 857 Master Registry companies out of 6,781,485; 2,138 source records with an INN unmatched. Chromium verified both `found` and `not_found` cards. Auto-update is NOT_CONFIGURED.

---

# Development Order

1.

Audit current models.

2.

Entity Registry.

3.

Relationship Registry.

4.

Facts Layer.

5.

Dataset Quality.

6.

Evidence Layer.

7.

Tests.

---

# Wave 1 Handoff

- W1-001 CBR Warning List: ACCEPTED on user's Mac by six gates.
- W1-002 CBR FinOrg: LIVE + CACHE PASS на Mac; отдельная реальная PostgreSQL/Chromium приёмка на CI PASS (run 35129041426, 435 tests). Полные SIX GATES на пользовательском Mac пока NOT CONFIRMED. Команда и доказательства: docs/W1_002_ACCEPTANCE.md.
- W1-003 ФНС — МСП, получатели поддержки: ACCEPTED / SIX GATES PASS на пользовательском Mac 16.09.2026, code de32573 (PR #29). Snapshot 15.09.2026; PostgreSQL 11 925 998 фактов ЮЛ/ИП; Master Registry 6 781 485, exact-INN связаны 1 897 870 сущностей. Подробности: docs/W1_003_ACCEPTANCE.md.
- W1-004 Росздравнадзор / W1-005 Роскомнадзор / W1-006 НОСТРОЙ, СРО: NOT STARTED.
- RNP/EIS: deferred until official access.
- DaMIA: not connected.
- Person / Leads / CRM / Enterprise: do not start.

---

# Success Definition

The existing official datasets become one unified business model capable of supporting:

- Checks
- Monitoring
- Executive Summary
- AI Explain

without additional redesign.

---

# Next Phase

Phase 5

Check Registry & Risk Engine

---

Status

APPROVED


### W1-002 final Mac acceptance — 16.09.2026
W1-002 is ACCEPTED / SIX GATES PASS on the user Mac. Real found exact-INN: 9706063520 (БАНК ЭЛЕМЕНТ (ООО), 2 licences/rights); real not_found: 9102309919. PostgreSQL kontragent reread PASS; Chromium found/not_found HTTP 200, page_errors=[]; 435 tests PASS. On-demand coverage is dated cache coverage, not a bulk CBR registry. auto_update=NOT_CONFIGURED. Source protocol: docs/W1_002_ACCEPTANCE.md.

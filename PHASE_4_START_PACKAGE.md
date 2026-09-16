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

Approved clarification (2026-09-16): the previously agreed Wave 1 sources are completed and accepted before SEO MVP. W1-001 CBR Warning List has now passed the permanent six-gate acceptance protocol on the user's Mac/PostgreSQL/Chromium environment. W1-003 is the next planned source but remains NOT STARTED until a separate explicit command in the new working chat. This clarification does not authorize arbitrary new sources or an architecture rewrite.

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
- W1-002 CBR FinOrg: LIVE + CACHE confirmed for INN `7707083893`; browser acceptance is a separate status and is not inferred from W1-001.
- W1-003 FNS SME support recipients: NEXT PLANNED, NOT STARTED. Begin only on explicit user command.
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

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

Approved clarification (2026-09-16): the previously agreed Wave 1 sources are completed and accepted before SEO MVP. Current work is W1-001 acceptance. W1-003 must not start without a separate command. This exception does not authorize arbitrary new sources or an architecture rewrite.

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

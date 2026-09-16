# Roadmap Changes After Audit

Version: 1.0

Status: APPROVED

Date: 2026-09-16

---

## Purpose

This document records all approved architectural and product changes introduced after completion of:

- Architecture Audit
- Business & Scale Audit
- Founder Audit

This document supplements ROADMAP.md.

---

## Main Decision

The project architecture is NOT rewritten.

The project evolves on top of the existing foundation.

Existing foundation remains:

- FastAPI
- PostgreSQL
- Master Registry
- Contracts
- Evidence
- Aggregators
- Ingestion
- Dataset Registry

---

## Approved Product Evolution

Previous model

Official Sources

↓

Company Card

↓

User

New model

Official Sources

↓

Normalization

↓

Facts

↓

Evidence

↓

Checks

↓

Conclusions

↓

AI Explain

↓

User

---

## Approved Components

Official architecture now includes:

- Entity Registry
- Relationship Registry
- Facts Layer
- Check Registry
- Executive Summary
- AI Explain Layer

---

## Approved Product Order

1. Company
2. SEO MVP
3. Monitoring
4. Person
5. Leads
6. CRM
7. Enterprise

---

## Product Strategy

Product Led Growth becomes the primary commercial strategy.

Main funnel

Search

↓

Company Card

↓

Registration

↓

Monitoring

↓

Subscription

---

## Founder Rules

Rule 1

No approved release may be delayed.

Rule 2

Every new idea goes to Backlog.

Rule 3

Release before feature completeness.

---

## Status

Approved.

## Approved execution clarification — 2026-09-16

Phase 3 is completed in its agreed scope; Phase 4 is ACTIVE. Complete the agreed Wave 1 before SEO MVP. No product-order or architecture rewrite.

W1-001 CBR Warning List is accepted on the user's Mac by the permanent six-gate protocol. The local PostgreSQL import on 16.09.2026 stored 27,163 official records; 2,995 have usable INNs, 24,168 do not. Exact-INN coverage against the 6,781,485-row Master Registry linked 857 records to 857 master companies; 2,138 source records with an INN were unmatched. Real `found` and dated-snapshot `not_found` cards were opened in Chromium with HTTP 200 and no page errors. `auto_update` remains NOT_CONFIGURED. See `docs/W1_001_ACCEPTANCE.md`.

W1-002 CBR FinOrg remains confirmed as LIVE + CACHE for INN `7707083893`; its separate browser acceptance is not silently inferred from W1-001.

W1-003 — FNS SME support recipients — is the next planned Wave 1 source but is NOT STARTED by this documentation update. Start it only after a separate explicit command in the new working chat.

The six permanent acceptance criteria are tests, a real official-source match, PostgreSQL write/readback, absence/non-applicability/failure semantics, a real browser card, and record/date/coverage checks. These are result-verification criteria, not new architecture.

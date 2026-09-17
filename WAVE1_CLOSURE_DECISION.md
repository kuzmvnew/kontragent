# Wave 1 Closure Decision — Kontragent

Date: 2026-09-17
Status: **CLOSED / ACCEPTED BY EXPLICIT PROJECT DECISION**

## Decision

Wave 1 is officially closed and accepted by explicit project decision of the product owner.

This is a project-governance closure, not a retroactive rewrite of source evidence. The factual source statuses remain preserved exactly:

- W1-001 CBR Warning List — **ACCEPTED / SIX GATES PASS**.
- W1-002 CBR FinOrg — **ACCEPTED / SIX GATES PASS**.
- W1-003 FNS SME support recipients — **ACCEPTED / SIX GATES PASS**.
- W1-004 Roszdravnadzor — **ACCEPTED / SIX GATES PASS**.
- W1-005 Roskomnadzor — **DEFERRED EXCEPTION / SOURCE-BLOCKED (B, C)**. Scope A/D/E/F is implemented and verified; official B/C responses remain incomplete/unavailable and are not treated as `not_found`. W1-005 is not relabeled as SIX GATES PASS.
- W1-006 NOSTROY / NOPRIZ / SRO — **ACCEPTED / SIX GATES PASS**.

## Closure rule

The unresolved W1-005 B/C source blocker is explicitly dispositioned as a deferred external-source exception and no longer blocks Wave 1 closure.

The blocker remains visible in backlog/status documentation and may be revisited when the official source becomes complete/stable. Reopening B/C later does not reopen Wave 1 unless a separate explicit project decision says so.

## Accepted technical checkpoint

At Wave 1 closure:

- GitHub main checkpoint before this documentation closure: `59d44082a6d71158ce79a2247a3d9cb4970cc581`.
- PostgreSQL database: `kontragent`.
- Alembic head: `e7a8b9c0d1e2`.
- W1-006 full regression: `472 passed`.
- Master Registry observed on user Mac: `6,781,487` entities.
- W1-006 Chromium: three real company cards, HTTP 200, `page_errors=[]`.
- Private SRO person evidence remains `PRIVATE_INTERNAL`; public person exposure confirmed as `0`.
- Auto-update remains a separate status and is not implied by Wave 1 closure.

## Permanent evidence rules retained

Wave 1 closure does not weaken the permanent source protocol:

1. automated tests;
2. real official response;
3. PostgreSQL write/read;
4. `found` / `not_found` / `not_applicable` / `unavailable` separation;
5. real Chromium/product check;
6. counts, dates and coverage.

Source errors, protection, timeout or incomplete official responses must never be converted to absence.

## Next stage

The immediate approved next stage is **Intermediate Stage 1.5** defined in `INTERMEDIATE_STAGE_1_5.md`.

Stage 1.5 exists before the full Risk Engine because the product must first:

- close final acceptance for FNS Tax Debt;
- close final acceptance for FNS Tax Offences;
- inventory/recover prior FSSP work;
- inventory/recover prior Fedresurs/EFРSB work;
- add/verify Courts v1 for arbitration and courts of general jurisdiction;
- model bankruptcy/liquidation as stages/events rather than a single flag;
- add FNS bank-account suspension decisions;
- complete the Bank of Russia public high-risk/KYC technical probe;
- add free-first corporate disclosure foundation;
- prepare core Company Card facts required later by Risk/Summary.

Stage 1.5 is not Wave 2.

After Stage 1.5:

1. Auto-update / Data Readiness;
2. Risk Engine;
3. Summary Engine;
4. Company Card v2;
5. Report v1;
6. Security & Resilience Gate;
7. Legal Launch Gate;
8. Product / Company Card Acceptance Gate;
9. first 10,000 SEO company pages;
10. Lists/Bulk, Monitoring/Event Engine and Workspace v1;
11. Wave 2 only at the approved later gate.

Project name remains **Kontragent**.

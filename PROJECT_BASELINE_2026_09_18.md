# Engineering Baseline — 2026-09-18

Status: **ENGINEERING BASELINE — PRODUCT RECOVERY PRE-ACCEPTANCE**

Immutability rule: **DO NOT UPDATE THIS FILE AFTER THIS BASELINE COMMIT**

This file freezes the canonical engineering handoff immediately before the
documentation-only baseline commit. Later changes must be recorded in dynamic
status documents, new dated evidence or a new baseline file.

## Repository control point

| Field | Frozen value |
|---|---|
| Repository | `kuzmvnew/kontragent` |
| Branch | `codex/risk-summary-product-recovery` |
| Implementation SHA before baseline docs | `6866aab4ba4ffdbd2f606a16be44e5b059a77e22` |
| Implementation commit | `fix: reconcile missing domain snapshot migrations` |
| Current `main` SHA | `510fa97792be43a4eb561288d2a2e78cd8764d79` |
| PR #44 | **OPEN / NOT MERGED** |
| Alembic head | `c0c90245de4b` |

`VERIFIED_FROM_GIT`, 2026-09-18 on the owner Mac: local `HEAD` and
`origin/codex/risk-summary-product-recovery` both resolved to
`6866aab4ba4ffdbd2f606a16be44e5b059a77e22`; `origin/main` resolved to
`510fa97792be43a4eb561288d2a2e78cd8764d79`; the worktree was clean.

`VERIFIED_RUNTIME`, 2026-09-18: the GitHub API reported
[PR #44](https://github.com/kuzmvnew/kontragent/pull/44) as `open`, with
`merged: false`.

## Canonical stage

- Product: **next.company / Kontragent**.
- Product type: **Russian B2B SaaS / counterparty due diligence**.
- Current stage: **PRODUCT RECOVERY V3 — NOT ACCEPTED**.
- Stage 1.6: **AUDIT CLOSED**.
- Company Card v2: **BLOCKED**.
- Product Recovery v3 architecture: **IMPLEMENTED**.

Completed stages at this point are Foundation, Master Registry, Phase 3 core
datasets within accepted scope, Wave 1, Intermediate Stage 1.5, Data Readiness,
historical Risk Engine v1, historical Summary Engine v1, Stage 1.6
audit/hardening and database schema reconciliation.

## Database and regression evidence

`VERIFIED_RUNTIME`, preserved in
`docs/SCHEMA_RECONCILIATION_2026-09-18.md`:

- a clean isolated PostgreSQL database reached Alembic `c0c90245de4b (head)`;
- schema completeness: **PASS**;
- ORM schema: **47/47 tables**;
- full isolated regression: **688 passed, 0 failed**;
- clean downgrade/upgrade round-trip: **PASS**.

The schema reconciliation repaired a historical migration-chain gap for
`company_tax_regime_snapshots` and
`company_revenue_expense_snapshots`. Alembic remains the single source of truth;
`Base.metadata.create_all` is not a production migration mechanism. Pytest must
not target the historical database named `kontragent`.

Historical database safety was verified without data loss:

| Table | Rows before | Rows after |
|---|---:|---:|
| `company_tax_regime_snapshots` | 5,673,177 | 5,673,177 |
| `company_revenue_expense_snapshots` | 1,753,134 | 1,753,134 |

The table OIDs, inspected schema, indexes, constraints and comments remained
unchanged; only `alembic_version` advanced to `c0c90245de4b`.

## Master Registry

At the Wave 1 closure control point, the owner Mac reported **6,781,487** Master
Registry entities. This is preserved historical evidence and does not rewrite
older dated acceptance reports.

## Golden-40 Product Recovery state

| Metric | Frozen value |
|---|---:|
| Golden cohort | **40/40** |
| Workflow Completion | **100%** |
| Evidence Coverage | **53–88/100** |
| Average Evidence Coverage | **65.3/100** |
| Positive gate | **0/40** |
| Low-risk positive gate | **0/10** |

Decision: **PRODUCT RECOVERY NOT ACCEPTED**.

## Source state and blockers

Terminal for all 40 companies: Registration, Tax debt, Tax offences, Finance,
CBR Warning, Management and ERKNM.

Non-gating classifications: Bankinform is `OPTIONAL_CONTEXT`; RNP/EIS is
`DEFERRED_EXTERNAL_ACCESS`.

| Mandatory gap | Frozen Golden-40 result |
|---|---|
| FSSP | **16 found; 24 unresolved** |
| EFRSB | **19 found; 21 unresolved** |
| CBR ZSK | **40 unresolved** |
| Arbitration | **1 partial; 39 unresolved** |
| General Courts | **19 partial; 21 unresolved** |
| Licences/SRO | **1 applicable unresolved** |

`UNAVAILABLE != NOT_FOUND`; `PARTIAL != CLEAN`. Firmoteka is an
`AUTHORIZED_BRIDGE`, not a Source of Truth, and must not be double-counted with
official evidence.

## Public runtime state

The verified Product Recovery public read contract at this baseline is:

- public company `GET`: **0 external provider calls**;
- public company `GET`: **0 database mutations**;
- known company HTML/API: controlled `200`;
- unknown company HTML/API: controlled `404`;
- mutation/internal routes: protected;
- internal implementation data: excluded from `PublicCompanyView`.

The preserved Stage 1.6 runtime profile recorded 62–69 SQL queries, p50
1035.026 ms and p95 1909.876 ms for the representative public-read checks.
Chromium desktop/mobile acceptance covered 40 cards with zero console errors.
See `PRODUCT_RECOVERY_V3_ACCEPTANCE.md` and
`STAGE_1_6_AUDIT_CLOSURE.md` for scope and limitations.

## Architecture invariants

Canonical pipeline:

`SOURCE → SOURCE ADAPTER → EVIDENCE → NORMALIZED FACT → DERIVED METRIC → RISK → SUMMARY → CLIENT VIEW`

Source precedence:

`OFFICIAL_DIRECT > OFFICIAL_DOWNLOADED_DATASET > AUTHORIZED_BRIDGE > DISCOVERY_ONLY`

Risk Score, Evidence Coverage and Workflow Completion are three distinct
0–100 values. Risk is not a probability; Coverage is not a source count;
Workflow Completion does not prove evidence exists.

## Roadmap position

Strict remaining recovery order:

1. FSSP closure.
2. EFRSB closure.
3. CBR ZSK closure.
4. Arbitration closure.
5. General Courts closure.
6. Final education/licence gap.
7. Golden-40 final acceptance.

Until Product Recovery is accepted, Company Card v2, Report v1, Platform 2.0,
10k enrichment, Security Gate v2, Legal Gate and SEO are blocked.

After acceptance, the approved sequence is:

`Recovery PR → main → close/supersede PR #44 after successful recovery merge → Company Card v2 → Report v1 → Platform 2.0 → SEO_COHORT_10000_V1 → 10k enrichment → Data Quality Gate → Production Candidate → Security & Resilience Gate v2 → Legal Launch Gate → Product Acceptance → SEO Canary 100 → SEO Canary 1000 → Public 10,000 → Commercial B2B Core`

## Evidence boundary

The evidence-class meanings are fixed by `EVIDENCE_REPORTING_POLICY.md`. Code
existence does not prove runtime behavior. Generic Engineering KB guidance does
not prove implementation and cannot override project-specific decisions. The
integration rules are in `ENGINEERING_KB_INTEGRATION.md`.

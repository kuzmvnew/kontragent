# AI Project Context — next.company / Kontragent

Status: **CANONICAL DYNAMIC PROJECT CONTEXT**

Last updated: **2026-09-18**

This is the first-read project context for Codex, AI coding agents, engineering
knowledge retrieval, code review and future maintainers. Keep it current when
the accepted project state changes. Do not use it to rewrite dated acceptance
evidence or the immutable baseline in `PROJECT_BASELINE_2026_09_18.md`.

## Product and current stage

- Product: **next.company / Kontragent**.
- Type: **Russian B2B SaaS / counterparty due diligence**.
- Current stage: **PRODUCT RECOVERY V3 — NOT ACCEPTED**.
- Stage 1.6: **AUDIT CLOSED**.
- Company Card v2: **BLOCKED** until Product Recovery is accepted.

The recovery architecture is implemented and the audit/hardening work is
closed. This is not product acceptance: mandatory evidence gaps remain, and
the Golden-40 positive gate is `0/40`.

## Canonical reading order

Before every substantial task, read:

1. `AI_PROJECT_CONTEXT.md`;
2. `PROJECT_STATUS.md`;
3. `EVIDENCE_REPORTING_POLICY.md`;
4. `ENGINEERING_KB_INTEGRATION.md`;
5. the relevant acceptance documents;
6. the relevant source passport.

Then retrieve only the relevant sections of the external Engineering Knowledge
Base. Do not load the entire corpus blindly. The immutable control point for
this handoff is `PROJECT_BASELINE_2026_09_18.md`.

## Completed project stages

| Stage | Canonical status |
|---|---|
| Foundation | **COMPLETE** |
| Master Registry | **COMPLETE** |
| Phase 3 core datasets | **COMPLETE within accepted scope** |
| Wave 1 | **CLOSED / ACCEPTED** |
| Intermediate Stage 1.5 | **CLOSED / ACCEPTED** |
| Data Readiness | **COMPLETE / ACCEPTED** |
| Risk Engine v1 | **COMPLETE / ACCEPTED historical** |
| Summary Engine v1 | **COMPLETE / ACCEPTED historical** |
| Product Recovery v3 architecture | **IMPLEMENTED** |
| Stage 1.6 audit/hardening | **CLOSED** |
| Database schema reconciliation | **COMPLETE / VERIFIED** |

Historical acceptance remains historical evidence. A later implementation or
runtime result does not silently rewrite an earlier dated checkpoint.

## Current Product Recovery state

The locked Golden cohort contains **40/40** companies.

| Metric | Current value |
|---|---:|
| Workflow Completion | **100%** |
| Evidence Coverage | **53–88/100** |
| Average Evidence Coverage | **65.3/100** |
| Positive gate | **0/40** |
| Low-risk positive gate | **0/10** |

Decision: **PRODUCT RECOVERY NOT ACCEPTED**. Do not weaken the gate, convert
blocked evidence into negative evidence, or begin a blocked future stage.

## Current source status

| Capability | Current state |
|---|---|
| Registration | **40 terminal** |
| Tax debt | **40 terminal** |
| Tax offences | **40 terminal** |
| Finance | **40 terminal** |
| CBR Warning | **40 terminal** |
| Management | **40 terminal** |
| ERKNM | **40 terminal** |
| Bankinform | **OPTIONAL_CONTEXT** |
| RNP/EIS | **DEFERRED_EXTERNAL_ACCESS** |
| FSSP | **16 found; 24 unresolved** |
| EFRSB | **19 found; 21 unresolved** |
| CBR ZSK | **40 unresolved** |
| Arbitration | **1 partial; 39 unresolved** |
| General Courts | **19 partial; 21 unresolved** |
| Licences/SRO | **1 applicable unresolved** |

`UNAVAILABLE` is not `NOT_FOUND`; `PARTIAL` is not `CLEAN`.

## Architecture invariants

The canonical data path is:

`SOURCE → SOURCE ADAPTER → EVIDENCE → NORMALIZED FACT → DERIVED METRIC → RISK → SUMMARY → CLIENT VIEW`

A new source must enter through the source/evidence/fact layers. It must not
directly rewrite Card, Risk, Summary, API or SEO behavior.

Source precedence is:

`OFFICIAL_DIRECT > OFFICIAL_DOWNLOADED_DATASET > AUTHORIZED_BRIDGE > DISCOVERY_ONLY`

Firmoteka is an **AUTHORIZED_BRIDGE**, not a Source of Truth. Bridge and direct
evidence must not be double-counted.

## Three distinct scores and states

- **Risk Score (0–100):** a deterministic risk score, not a probability.
- **Evidence Coverage (0–100):** evidence sufficiency under the coverage model,
  not a raw count of sources out of 100.
- **Workflow Completion (0–100):** completion of required checks, not proof that
  evidence was found.

Never combine or substitute these values.

## Evidence policy

Every completion or verification claim must use one of the evidence classes in
`EVIDENCE_REPORTING_POLICY.md`:

- `VERIFIED_FROM_GIT`;
- `VERIFIED_RUNTIME`;
- `USER_REPORTED`;
- `EXTERNAL_HISTORICAL_EVIDENCE`;
- `INFERENCE`;
- `UNVERIFIED`.

Code existence is not runtime verification. Preserve exact evidence semantics:
`UNAVAILABLE != NOT_FOUND` and `PARTIAL != CLEAN`.

## Public runtime invariants

- Public company `GET` performs **0 external provider calls**.
- Public company `GET` performs **0 database mutations**.
- An unknown company returns a controlled **404**.
- Mutation and internal routes are protected.
- `PublicCompanyView` does not expose internal implementation data.

## Database invariants

- Alembic is the single source of truth for database schema creation.
- A fresh database must be created completely by `alembic upgrade head`.
- Verified Alembic head at this context revision: `c0c90245de4b`.
- Verified schema: **47/47 ORM tables**.
- Never use `Base.metadata.create_all` as a production schema migration.
- Never run pytest against the main database named `kontragent`; use an isolated
  test database.

Historical root cause: the production-history database contained
`company_tax_regime_snapshots` and `company_revenue_expense_snapshots`, but the
migration chain did not create them. Revision `c0c90245de4b`, delivered in
commit `6866aab4ba4ffdbd2f606a16be44e5b059a77e22`, repaired clean provisioning
without data loss. CI now checks schema completeness. Full evidence is in
`docs/SCHEMA_RECONCILIATION_2026-09-18.md`.

## AI development loop

Every substantial change follows this sequence:

`CURRENT STATE → TASK CONTRACT → INVARIANTS → DEPENDENCIES → MINIMAL IMPLEMENTATION → TESTS → REAL RUNTIME EVIDENCE → DIFF REVIEW → STATUS UPDATE → COMMIT`

Passing tests alone is not completion. Do not use the shortcut “write code →
tests pass → declare complete.”

## Safe change rule

No Big Bang Rewrite. Split large work into bounded tasks. Each task needs:

- tests proportionate to the change;
- a real runtime check when runtime behavior is claimed;
- recorded evidence;
- a clear rollback point.

## Strict next-task order

1. FSSP closure.
2. EFRSB closure.
3. CBR ZSK closure.
4. Arbitration closure.
5. General Courts closure.
6. Final education/licence gap.
7. Golden-40 final acceptance.

## Blocked future work

Until Product Recovery is accepted, do not start:

- Company Card v2;
- Report v1;
- Platform 2.0;
- 10k enrichment;
- Security Gate v2;
- Legal Gate;
- SEO.

## Roadmap after Product Recovery acceptance

`Recovery PR → main → close/supersede PR #44 after successful recovery merge → Company Card v2 → Report v1 → Platform 2.0 → SEO_COHORT_10000_V1 → 10k enrichment → Data Quality Gate → Production Candidate → Security & Resilience Gate v2 → Legal Launch Gate → Product Acceptance → SEO Canary 100 → SEO Canary 1000 → Public 10,000 → Commercial B2B Core`

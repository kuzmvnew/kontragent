# Stage 1.6 — Product Recovery Audit Closure & Runtime Hardening

Status: **ACTIVE / REQUIRED — IN PROGRESS**

Last verified: **18.09.2026 (Europe/Moscow)**

Code checkpoint: `b8e115e` (`feat: complete stage 1.6 runtime hardening`)

## Position in the delivery sequence

`Product Recovery v3 → Stage 1.6 → Golden-40 Acceptance → Product Recovery ACCEPTED → Recovery PR / main → Company Card v2 → Report v1 → Platform 2.0 → 10k enrichment → Data Quality Gate → Production Candidate → Security & Resilience Gate v2 → Legal Gate → Product Acceptance → SEO`

Stage 1.6 blocks Company Card v2, Report v1, Platform 2.0 implementation, 10k enrichment, the formal Security and Legal gates, SEO and Wave 2. No blocked stage was started in this work.

## Evidence classes

- `VERIFIED_FROM_GIT`: branch preflight started from local/origin `f5f77d687d9fa38849ae289f47828a09bca3b666`; implementation is in code checkpoint `b8e115e`.
- `VERIFIED_RUNTIME`: migration cycle, tests, Golden-40, browser QA, localhost smoke, query plans and profiling described below were run on the user Mac.
- `USER_REPORTED`: the product owner reports authorization for low-load automated use of Firmoteka and public verification flows for FSSP, EFRSB/Fedresurs and CBR ZSK. Codex did not inspect a contract or independently verify those rights.
- `EXTERNAL_BRIDGE_EVIDENCE`: Firmoteka facts remain bridge evidence with preserved provenance; they are never relabelled as official direct evidence.

## Stage 1.6 workstreams

| Workstream | Status | Verified result |
|---|---|---|
| 1.6-A Source Runtime Completion | **IN PROGRESS** | Every mandatory Golden-40 workflow reached a terminal state, but several sources remain unavailable or partial. |
| 1.6-B Firmoteka Architecture Completion | **IMPLEMENTED / VERIFIED** | Source class and provenance are preserved, precedence is deterministic, and bridge/direct facts are resolved without risk double counting. |
| 1.6-C Public Read Safety | **IMPLEMENTED / VERIFIED** | Public company GET is DB/cache-only; unknown companies return 404 without provider calls or DB writes. |
| 1.6-D Sync/Async Runtime Safety | **IMPLEMENTED / VERIFIED** | Blocking DB/provider route handlers are synchronous and therefore run in FastAPI's threadpool. |
| 1.6-E API Boundaries | **IMPLEMENTED / VERIFIED** | Public, authenticated and internal contracts are separate; `/api/company/{inn}` returns the public projection. |
| 1.6-F Minimal Access Control | **IMPLEMENTED / VERIFIED BASELINE** | Mutation and `/internal/*` routes require an env-backed fail-closed credential. This is not the formal Security Gate. |
| 1.6-G Risk/Coverage/Summary Quality | **IMPLEMENTED / VERIFIED** | Risk, evidence coverage and workflow completion are separate; client copy and source-specific positive wording are present; positive conclusions remain gated. |
| 1.6-H Search Core | **IMPLEMENTED / VERIFIED** | PostgreSQL search supports exact/prefix INN, exact OGRN, normalized names, ranking, typo search and autocomplete. |
| 1.6-I Company Read Profiling | **MEASURED / OPTIMIZED** | Query fan-out fell from 90–91 to 75–78; external calls stayed 0. Residual fan-out is recorded, not hidden. |
| 1.6-J CI / Release Baseline | **IMPLEMENTED / VERIFIED LOCALLY** | Migration, diff, Ruff critical checks, compile, secret scan, dependency audit, pytest and startup smoke are in CI and passed locally. |
| 1.6-K Golden-40 Acceptance | **IN PROGRESS** | 40/40 processed and workflow completion is 100%, but evidence coverage is 47–72/100 and the positive gate is 0/40. |

## Runtime and performance evidence

- Tests: **648 passed**, exit 0; two third-party deprecation warnings and one interpreter-shutdown psycopg `ResourceWarning` were observed. Previous baseline was 639.
- Alembic: downgrade `b8c9d0e1f2a3 → a7b8c9d0e1f2`, upgrade to `b8c9d0e1f2a3 (head)`, then head verified.
- Search plans: `ix_companies_inn_prefix` for exact/prefix INN, existing `ix_companies_ogrn` for OGRN, `ix_companies_name_lower` for exact name, and `ix_companies_name_lower_trgm` for typo search. Recorded execution times were 0.594 ms, 0.704 ms, 0.673 ms, 0.961 ms and 24.039 ms respectively.
- Five-company public read profile after batching: **75–78 SQL queries**, p50 **1098.022 ms**, p95 **1676.589 ms**, **0 external calls**, payload **715–4057 bytes**. Before batching: 90–91 queries, p50 1033.176 ms, p95 1653.371 ms. The query reduction is verified; this small sample does not prove a latency improvement.
- Local HTTP smoke: health 200, normalized search 200, known public company 200, unknown company 404, anonymous mutation 401, anonymous internal route 401, credentialed internal route 200.
- Browser QA: desktop and 390×844 mobile rendered; 40 cards present; search reduced the view to 1/40; category filtering reduced it to 10/40; risk, coverage, workflow, limitations, recommendations and source details were visible. Internal enum/dataset/rule identifiers were absent from the first view.

Evidence artifacts:

- `artifacts/stage_1_6/golden_40/product_recovery_v3_40.{json,csv,html}`
- `artifacts/stage_1_6/golden_40/product_recovery_v3_40_report.json`
- `artifacts/stage_1_6/search_explain.json`
- `artifacts/stage_1_6/company_read_profile.json`
- `artifacts/stage_1_6/company_read_profile_before_optimization.json`

## Golden-40 result

- Cohort: the same 40 companies, with the previously proven GGP selector correction only.
- Buckets: 10 bankruptcy, 10 tax, 10 active, 10 low-risk candidates.
- Processed: **40/40**.
- Workflow Completion: **100–100%**.
- Evidence Coverage: **47–72/100**, average **57.8/100**.
- Positive gate: **0/40 overall; 0/10 in the low-risk candidate bucket**.
- Persistence: 40 new immutable v3 assessment/summary pairs for summary engine 3.1.1; no stale summary-version reuse.
- FSSP validation: 36 not in the validation matrix, 3 unverified, 1 unresolved mismatch (`0274101890`, bridge 74 versus historical direct 96).

The Golden-40 gate did not pass. Product Recovery remains **IN PROGRESS**; rules were not weakened to manufacture a positive result.

## Exact unresolved blockers

| Capability | Companies unresolved | Runtime reason | Needed to close |
|---|---:|---|---|
| Arbitration | 40/40 | Checko credential absent; no approved direct KAD machine transport completed the run. | Provide an authorized env credential or approved official machine path, then rerun all 40. |
| General courts | 40/40 | Region routing exists, but official regional result access remains challenge/empty-response limited and Moscow-only coverage is not treated as nationwide. | Authorized regional access plus exact-identifier adapters and cross-region runtime evidence. |
| CBR ZSK | 40/40 | Public flow requires interactive confirmation; no completed authorized session was available. | Complete an authorized per-company interactive/machine flow and preserve terminal evidence. |
| FNS Bankinform | 40/40 | The service requires a real BIK; arbitrary BIK substitution is prohibited. | Supply verified company/bank context and BIK semantics, then execute the official flow. |
| FSSP | 24/40 | No configured direct machine transport and no sufficient bridge result for those companies. | Run the authorized official transport, finish direct-vs-bridge validation and resolve the 74/96 mismatch. |
| Bankruptcy / EFRSB | 21/40 | No configured direct EFRSB transport and no source-backed bridge event for those companies. | Configure the authorized official transport and rerun with canonical event evidence. |
| Licences/SRO | 25/40 | Bridge coverage is absent or partial. | Add proven applicable official/authorized coverage without converting absence of data into `not_found`. |
| Procurement/RNP | 40/40 | Official EIS access remains deferred. | Obtain approved official access and complete the capability. |
| Regulatory inspections | 40/40 | No resolved current capability evidence in this acceptance matrix. | Define/apply the mandatory profile and provide current official evidence or honest applicability. |
| Positive conclusion | 40/40 | Evidence coverage and mandatory hard-check resolution do not meet the positive gate. | Close the relevant source gaps and rerun the same cohort; do not change rules merely to pass. |

## Security and future platform boundary

Stage 1.6 provides only the baseline: read safety, API projection, internal-route protection, secret scanning and CI smoke. It does **not** pass Security & Resilience Gate v2.

Platform 2.0 is a future required stage after Company Card v2 and Report v1, and before 10k enrichment. Its approved scope is Source SDK, canonical Evidence/Facts/Derived Metrics, Company Projection and versioning, dependency graph and incremental recalculation, Enrichment Orchestrator, durable idempotent jobs/workers, anomaly detection and quarantine, production scheduler, observability, backup/restore and load infrastructure. Current non-goals are Kubernetes, Kafka, OpenSearch, RabbitMQ unless Stage 1.6 objectively requires it, microservices and a full async rewrite.

Public MVP and Commercial B2B MVP are separate milestones. Public MVP comprises Search, Card, Risk, Summary, Report, Platform 2.0, 10k enrichment, Security, Legal, Product Acceptance and SEO. Commercial B2B MVP adds account/workspace, saved lists, bulk checks, monitoring, notifications, export and minimal tariff/usage controls; it is not being built now.

## Decision

**STAGE 1.6 IN PROGRESS. PRODUCT RECOVERY V3 IN PROGRESS.**

The implementation and audit-hardening work is verified, but source-runtime completion and the Golden-40 positive gate are not complete. Company Card v2 and every later blocked stage remain blocked.

# Product Recovery v3 — integration acceptance report

Date: 2026-09-17
Decision: **PRODUCT RECOVERY IN PROGRESS**

Evidence labels follow `EVIDENCE_REPORTING_POLICY.md`.

## Verified control point

- Branch: `codex/risk-summary-product-recovery`.
- Git and GitHub branch SHA: `3ed0506ce372eaaf7bb432b6f3093a137c0a1bf5`.
- `VERIFIED_RUNTIME` on the product owner's Mac: Alembic `a7b8c9d0e1f2 (head)` and full regression `639 passed`.
- Local SHA equalled origin SHA, `git diff --check` passed and the worktree was clean.
- This control point verifies the committed implementation and regression state. It does not change the Product Recovery decision to accepted.

## Code existence versus runtime completion

`CODE EXISTS` and `RUNTIME VERIFIED` are separate claims. The source catalog,
governor, adapters, runners, resolver and v3 engines exist in code and pass the
recorded regression. Runner presence does not mean that a real source completed
all 40 companies. Only the results explicitly recorded in the runtime matrix
below are treated as completed runtime evidence.

## A. Architecture changes

- `VERIFIED_FROM_GIT`: the pipeline now contains `SourceCapabilityCatalog → SourceRunnerRegistry → SourceRateGovernor → SourceResolver → NormalizedCheckResult → Coverage Engine v2 → Risk Engine v3 → Summary Engine v3`.
- The previous v2 contracts remain compatible while `CompanyCheckResult` exposes normalized results and v3 projections.
- Company Card v2, Report v1, SEO, Wave 2 and the 100k run were not started.

## B. Capability catalog

- The catalog contains 15 real product capabilities, not a reconstructed workbook or “100 sources”.
- It maps accepted runtime paths for registration, bankruptcy, FSSP, tax, finance, courts, compliance, management, licences/SRO, RNP and inspections.
- `100` means normalized coverage points. Dataset presence does not complete a company check.

## C–E. Firmoteka, precedence and rate governor

- `FirmotekaSourceAdapter` emits normalized, evidence-hashed `AUTHORIZED_BRIDGE` results.
- Firmoteka is not a Source of Truth. Authorization for its automated parsing is `USER_REPORTED`; no contract or authorization letter was independently inspected.
- Exact-INN registration can close registration. Tax/finance remain secondary to FNS.
- Firmoteka score is excluded from Risk v3.
- Resolver applies `OFFICIAL_DIRECT > OFFICIAL_DOWNLOADED_DATASET > AUTHORIZED_BRIDGE > DISCOVERY_ONLY` and prevents double counting.
- Rate governor implements 6–12 second Firmoteka intervals, per-source/per-IP/global budgets, stable worker/shard identity, cache, retry/backoff, Retry-After, circuit breaker and checkpoint/resume.

## F–J. Direct runners and courts

- `FsspDirectRunner`, `EfrsbDirectRunner`, `CbrZskRunner` and `FnsBankinformRunner` are implemented as fail-closed adapters over injectable authorized transports.
- Authorization for automated public flows of FSSP, EFRSB/Fedresurs and CBR ZSK is recorded only as `USER_REPORTED`; no contract or authorization letter was independently inspected.
- They never solve/bypass CAPTCHA, store challenge secrets or convert unavailable into not-found.
- ZSK exposes only high-risk-information found/not-found. It does not invent low/medium groups.
- Bankinform requires an explicit BIK; no arbitrary BIK is silently hardcoded.
- `ArbitrationResolver` applies source precedence. Existing Checko path remains the primary configured runner.
- `VERIFIED_RUNTIME`: `CHECKO_API_KEY` was absent, so 40/40 arbitration refresh was not claimed.
- General-court targeted coverage remains partial and contributes zero risk points without actual case facts.

## K. Risk Engine v3

- Version `risk-engine-3.0.2`; explainable product risk index from 0 to 100. It is not a probability of default, bankruptcy or fraud and is not a credit rating.
- Section maxima follow the approved 12/18/12/14/12/10/10/5/3/2/2 allocation.
- Exact bridge evidence strength defaults to 0.90; discovery-only contributes zero.
- Active official bankruptcy creates a score floor of 90. Strong active bridge evidence is visible as bridge and creates a high-risk floor, not a direct-source claim.
- Coverage never multiplies or suppresses risk.
- One resolved fact per capability prevents direct/bridge double counting.
- Every score contribution remains traceable to source, fact, rule, calculation and date.

## L. Coverage Engine v2

- Version `coverage-engine-2.0.0`; weighted completeness score normalized to 100. It is not a count of sources out of 100.
- `NOT_APPLICABLE` is removed from the denominator.
- Partial results receive only fractional weight.
- Authorized bridge closes a capability only where catalog policy allows it; negative FSSP bridge does not close FSSP before validation.
- Mandatory score and hard-check gate are separate fields.

## M. Summary Engine v3

- Version `summary-engine-3.0.2`.
- First screen separates risk and coverage, uses up to three reasons, source-specific positive checks, grouped limitations and actionable recommendations.
- User-facing output uses Russian labels, `₽` instead of `RUB` and `ДД.ММ.ГГГГ` dates.
- A positive conclusion requires coverage ≥90, every mandatory hard check resolved and risk score <20.
- Source details include source type, date, coverage, rule and arithmetic risk points.

## N. 40-company runtime matrix

`VERIFIED_RUNTIME`, local PostgreSQL and cached Firmoteka pilot, current run:

- 40/40 unique INN: bankruptcy 10, tax 10, ordinary active 10, high-coverage candidates 10.
- GGP (`7730709480`) was removed from the bankruptcy bucket and replaced by a cached candidate with a concrete bankruptcy event.
- Coverage min/max/average: 47/80/58.0.
- Risk labels: 21 low observed, 2 moderate, 1 attention, 16 high. No direct active bankruptcy was completed, so no official-direct critical floor was claimed.
- Positive conclusion gate: 0/40. The low-risk/high-coverage bucket is therefore **not accepted**.
- PostgreSQL: immutable v3 assessment/summary pairs were written for each acceptance revision; browser wording and money-format fixes created newer 3.0.1/3.0.2 rows instead of rewriting historical rows.

## O. Exact unresolved blockers

| Capability | Unresolved in 40 | Current blocker |
|---|---:|---|
| Arbitration | 39 | `CHECKO_API_KEY` absent; no alternative current official result |
| General courts | 40 | nationwide resolver not available; existing path is targeted/partial |
| CBR ZSK | 40 | interactive public flow not completed in this run |
| FNS Bankinform | 40 | company-specific valid BIK not supplied to the flow |
| FSSP | 24 | direct sessions not completed; negative bridge cannot close capability yet |
| Bankruptcy | 21 | no direct EFRSB transport; only ten selected concrete bridge events plus existing official facts where available |
| Licences/SRO | 25 | no applicable structured result for those companies |
| RNP | 40 | official access pending; optional profile capability |
| Regulatory inspections | 40 | no company-level resolved result in this acceptance payload; optional profile capability |

These counts are capability resolution counts, not source inventory counts.

## P. Tests

- `VERIFIED_RUNTIME`: targeted source/v3 regressions: 20 passed.
- `VERIFIED_RUNTIME`: implementation-run full suite: 639 passed in 1.68s.
- `VERIFIED_RUNTIME`: subsequent Mac verification at committed SHA `3ed0506ce372eaaf7bb432b6f3093a137c0a1bf5`: 639 passed; Alembic `a7b8c9d0e1f2 (head)`.
- Covered: Firmoteka normalized results, precedence, no double count, bridge fallback, FSSP direct precedence, GGP regression, ZSK, Bankinform, Checko 40 runner path, weighted coverage, `NOT_APPLICABLE`, partial court semantics, risk arithmetic/floors, low-coverage gate, positive gate, specific wording and no client jargon.

## Q. PostgreSQL

- Migration `a7b8c9d0e1f2` adds immutable `company_risk_assessments_v3` and `company_summaries_v3` tables.
- `VERIFIED_RUNTIME`: upgrade → downgrade to `f6a7b8c9d0e1` → upgrade completed successfully.
- `VERIFIED_RUNTIME`: current acceptance wrote and read 40 linked v3.0.2 assessment/summary rows. Earlier 3.0.0 and 3.0.1 browser-QA revisions remain immutable (40 pairs each).

## R. Chromium

- `VERIFIED_RUNTIME`: first 1440×1200 and 390×844 render revealed internal wording leaks (`competitive_proceedings`, `INACTIVE`, `not_checked`) and scientific money notation.
- The leaks were fixed. Final v3.0.2 Chromium desktop/mobile reruns completed with exit code 0; screenshots were visually inspected.
- Final HTML contains 40 cards, 40 source-detail disclosures and five filter controls. The acceptance scan found zero forbidden/internal tokens, including the three discovered leaks and scientific `e+` notation.

## S. Git status

- Recovery implementation was committed and pushed on `codex/risk-summary-product-recovery` as `3ed0506ce372eaaf7bb432b6f3093a137c0a1bf5`.
- The product owner's Mac verified that local SHA equals origin SHA, `git diff --check` passes and the worktree is clean.
- No Recovery PR was created. PR #44 remains open and unmerged.

## T. Remaining Product Recovery work

1. Operationalize FSSP runtime.
2. Operationalize EFRSB runtime.
3. Operationalize CBR ZSK.
4. Operationalize FNS Bankinform.
5. Resolve Arbitration for 40/40 companies.
6. Improve General Courts coverage.
7. Rerun the same 40-company regression.
8. Raise mandatory Coverage and open the correct positive gate.
9. Complete Product Recovery acceptance before starting Company Card v2.

## Decision

**PRODUCT RECOVERY IN PROGRESS.**

The architecture and v3 engines are implemented and exercised, but the required
40/40 real direct runners and mandatory coverage 100/100 are not available in
this environment. Reporting `PRODUCT RECOVERY ACCEPTED` would be false.

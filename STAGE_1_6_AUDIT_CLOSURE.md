# Stage 1.6 — Product Recovery Audit Closure & Runtime Hardening

Status: **AUDIT CLOSED / PRODUCT RECOVERY NOT ACCEPTED**

Last verified: **18.09.2026 (Europe/Moscow)**

Code checkpoint: `d8b1d38` (`feat: close product recovery source runtime gaps`)

## Decision

The mandatory audit, source-forensics and runtime-hardening work is complete. Product Recovery itself is **not accepted**: Golden-40 positive conclusions remain 0/40 because mandatory FSSP, EFRSB, ZSK and court evidence is unavailable or incomplete. No source precedence, threshold, risk weight or acceptance rule was weakened. Company Card v2 and every later phase remain blocked.

## Evidence classes

- `VERIFIED_FROM_GIT`: branch, code and commit facts.
- `VERIFIED_RUNTIME`: tests, migrations, Golden-40, browser, public runtime, performance and generated audit artifacts.
- `EXTERNAL_HISTORICAL_EVIDENCE`: the preserved 2026-09-17 official FSSP exact-INN result.
- `INFERENCE`: 74 current + 22 completed = 96 explains the aggregate FSSP count difference.
- `UNVERIFIED`: FSSP row identity and amount equivalence; any negative conclusion from blocked sources.

## Acceptance evidence

- Full regression: **671 passed**, exit 0; two third-party deprecation warnings and an interpreter-shutdown psycopg `ResourceWarning` were observed.
- Alembic: downgrade from `b8c9d0e1f2a3` to `a7b8c9d0e1f2`, then upgrade back to `b8c9d0e1f2a3 (head)`.
- Local checks: diff check, critical Ruff, compileall, secret scan, dependency audit and startup smoke passed.
- Remote CI: **NOT TRIGGERED** because no pull request or `main` update was authorized.
- Golden cohort: unchanged **40/40**; workflow **100–100%**; coverage **53–88**, average **65.3**; positive gate **0/40**, low-risk **0/10**.
- Golden delta: unavailable terminal states **212 → 186**; unresolved mandatory occurrences **172 → 146**.
- Persistence: **40** new immutable Risk/Summary v3 pairs and **0** stale-version reuses in the final run.
- Public runtime: known HTML/API `200`, unknown HTML/API `404`, exact search `200`, anonymous mutation/internal `401`, authorized internal `200`, **0** DB writes, **0** external calls and no test token in logs.
- Browser: Chromium 153 desktop/mobile; 40 cards; exact-INN search 1/40; each four group filters 10; three score fields, reasons, limitations and source tables on every card; no horizontal overflow; zero console errors.
- Read path: **62–69** SQL queries and **0** external calls. p50 **1035.026 ms**, p95 **1909.876 ms** versus prior **1012.274/1578.178 ms**. The latency regression is recorded as a post-migration cold/outlier effect with a larger immutable local history; there is no query-count or network regression.

## Capability closure matrix

| CAPABILITY | BEFORE | AFTER | OFFICIAL RESULT | BRIDGE RESULT | FINAL STATUS | GOLDEN RESOLVED | BLOCKER |
|---|---|---|---|---|---|---:|---|
| FSSP | FOUND=16, UNAVAILABLE=24 | FOUND=16, UNAVAILABLE=24 | FOUND=1, UNAVAILABLE=24 | FOUND=15 | BLOCKED | 16 | Live exact-INN flow returned CAPTCHA; negative bridge is not accepted |
| Bankruptcy / EFRSB | FOUND=19, UNAVAILABLE=21 | FOUND=19, UNAVAILABLE=21 | UNAVAILABLE=21 | FOUND=19 | BLOCKED | 19 | Official REST credentials are absent; bridge absence is not negative evidence |
| CBR ZSK | UNAVAILABLE=40 | UNAVAILABLE=40 | UNAVAILABLE=40 | NO_RESULT | BLOCKED | 0 | SmartCaptcha; no completed Golden-40 sessions |
| Arbitration | UNAVAILABLE=40 | PARTIAL=1, UNAVAILABLE=39 | NO_RESULT | PARTIAL=1, UNAVAILABLE=39 | PARTIAL | 1 | Checko credential absent; authorized KAD machine path unconfirmed |
| General courts | UNAVAILABLE=40 | PARTIAL=19, UNAVAILABLE=21 | PARTIAL=19, UNAVAILABLE=21 | NO_RESULT | PARTIAL | 19 | Regional/targeted rather than nationwide coverage; 21 routes incomplete |
| Licences/SRO | FOUND=15, N/A=18, UNAVAILABLE=7 | FOUND=15, N/A=18, PARTIAL=6, UNAVAILABLE=1 | PARTIAL=6, UNAVAILABLE=1 | FOUND=15 | PARTIAL | 39 | One education-licence path is not connected |
| RNP/EIS | UNAVAILABLE=40 | UNAVAILABLE=40 | NO_RESULT | UNAVAILABLE=40 | CLOSED BY POLICY | 40 | Deferred external-access capability, not current acceptance denominator |
| ERKNM | FOUND=1, NOT_FOUND=39 | FOUND=1, NOT_FOUND=39 | FOUND=1, NOT_FOUND=39 | NO_RESULT | CLOSED | 40 | None in cohort |

The machine-readable table is `artifacts/product_recovery_final/capability_delta.json`.

## Source-gap closure and forensics

- EFRSB: the official v1.4 production REST path (`/v1/auth`, `/v1/bankrupts`, `/v1/messages`) is implemented with credentials read only from environment, exact-INN verification, bounded pagination and secret-safe errors. Missing credentials produce access-pending, not absence.
- FSSP: bridge evidence now preserves case rows and count/amount semantics. For INN `0274101890`, official count 96 and bridge 74 current + 22 completed reconcile only at aggregate level. Official amount covers 85/96 visible rows and is not comparable with bridge total/remaining amounts.
- General courts: 40 region-aware attempts produced 3 found, 16 not-found and 21 unavailable at provider level; v3 conservatively exposes 19 partial and 21 unavailable. Provider URLs and codes are retained.
- Arbitration: one recent exact bridge snapshot is accepted only as partial for the shifted requested period; normalized temporal/page coverage is explicit and query fan-out is unchanged.
- Licences/SRO: six applicable NOSTROY checks completed (3 found, 3 not-found), reducing unavailable gaps from seven to one.
- CBR ZSK: no authorized machine route was found; SmartCaptcha remains a real blocker.

## Data-quality report

The final scan checks extreme debt/revenue ratios, impossible/future dates, negative amounts, duplicate court/enforcement records, stale sources, entity mismatch, source-class mismatch and bridge/direct conflict. It found one retained FSSP conflict. Its count semantics are explained, while row/amount equivalence stays unresolved. No other listed anomaly was found in the normalized Golden-40 matrix.

## Engineering quality report

- New tests since the prior checkpoint: **16** (655 → 671).
- Source classification corrected: Checko moved from official-direct to authorized bridge.
- Duplicate query removed: exact and shifted-period arbitration cache selection now use one ordered SQL query, preserving the **62–69** query envelope.
- Secret handling: EFRSB credentials/JWT are absent from URLs, evidence and error text; staged tracked-file secret scan passed.
- No schema migration was added; existing downgrade/upgrade path remains valid.
- Artifacts are deterministic audit outputs except timestamps and immutable persistence identifiers.

## Final blockers and ordered action

Open mandatory blockers are FSSP 24, EFRSB 21, CBR ZSK 40, arbitration 39, general courts 21 and licences/SRO 1. Resume Product Recovery only after authorized source access or equivalent official evidence changes these facts, then rerun the same Golden-40 cohort under the unchanged gate. Do not start Company Card v2, Report v1, Platform 2.0, 10k enrichment, formal launch gates, SEO or Wave 2.

## Evidence artifacts

- `artifacts/product_recovery_final/product_recovery_v3_40.{json,csv,html}`
- `artifacts/product_recovery_final/product_recovery_v3_40_report.json`
- `artifacts/product_recovery_final/golden_delta.json`
- `artifacts/product_recovery_final/capability_delta.json`
- `artifacts/product_recovery_final/fssp_direct_bridge_comparison.json`
- `artifacts/product_recovery_final/general_courts_runtime.json`
- `artifacts/product_recovery_final/data_quality_anomalies.json`
- `artifacts/product_recovery_final/source_runtime_status.json`
- `artifacts/product_recovery_final/runtime_safety.json`
- `artifacts/product_recovery_final/company_read_profile.json`
- `artifacts/product_recovery_final/browser_acceptance.json` and `browser/*.png`

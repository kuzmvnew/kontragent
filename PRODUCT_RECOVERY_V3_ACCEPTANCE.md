# Product Recovery v3 — final Stage 1.6 acceptance report

Date: 2026-09-18

Decision: **PRODUCT RECOVERY NOT ACCEPTED**

Code checkpoint: `d8b1d38` (`feat: close product recovery source runtime gaps`)

Evidence policy: runtime statements below are `VERIFIED_RUNTIME`; repository and commit statements are `VERIFIED_FROM_GIT`; the preserved FSSP result is `EXTERNAL_HISTORICAL_EVIDENCE`; the 74 + 22 count reconciliation is `INFERENCE`; row-level and amount equivalence remain `UNVERIFIED`.

## Verified engineering result

- Canonical source precedence remains `OFFICIAL_DIRECT > OFFICIAL_DOWNLOADED_DATASET > AUTHORIZED_BRIDGE > DISCOVERY_ONLY`; Checko is correctly classified as a bridge.
- The documented official EFRSB REST route is implemented with credential-only activation, exact-INN matching, bounded pagination and secret-safe errors. Production credentials are absent, so it remains access-pending rather than falsely negative.
- Protected FSSP/ZSK/Bankinform result vocabularies normalize correctly only after completed sessions.
- Arbitration can conservatively reuse a recent shifted-period snapshot as partial coverage without adding a SQL query.
- General-court provenance now retains the actual regional provider and never presents regional absence as nationwide absence.
- Firmoteka FSSP evidence preserves current/completed/closed counts, total/remaining amounts, production rows and snapshot metadata.
- Public reads remain DB/cache-only: known HTML/API `200`, unknown HTML/API `404`, exact search `200`, anonymous mutation/internal `401`, authorized internal `200`, zero writes and zero provider calls.
- Full regression: **671 passed**; Alembic downgrade/upgrade returned to `b8c9d0e1f2a3 (head)`; critical Ruff, compileall, secret scan, dependency audit and startup smoke passed locally.
- Remote CI was not triggered because no pull request or `main` update was authorized.

## Golden-40 delta

| Metric | Before | After |
|---|---:|---:|
| Companies | 40 | 40 |
| Workflow completion | 100–100% | 100–100% |
| Coverage | 51–78 | 53–88 |
| Average coverage | 63.5 | 65.3 |
| Positive conclusions | 0/40 | 0/40 |
| Low-risk positive gate | 0/10 | 0/10 |
| Unavailable terminal states | 212 | 186 |
| Unresolved mandatory occurrences | 172 | 146 |

The same cohort and unchanged acceptance gate were used. The low-risk candidates have coverage 53–54, average 53.3, so a positive conclusion would be false.

## Remaining mandatory gaps

| Capability | Final Golden-40 result | Honest state |
|---|---:|---|
| FSSP | 16 found; 24 unavailable | Blocked for negative closure |
| Bankruptcy / EFRSB | 19 found; 21 unavailable | Blocked pending official API credentials |
| CBR ZSK | 40 unavailable | Blocked by SmartCaptcha / no completed sessions |
| Arbitration | 1 partial; 39 unavailable | Partial bridge coverage |
| General courts | 19 partial; 21 unavailable | Partial regional official coverage |
| Licences/SRO | 18 N/A; 15 found; 6 partial; 1 unavailable | Partial; one education-licence path missing |

RNP remains a separately reported deferred external-access capability. Bankinform is contextual N/A for this cohort. ERKNM is terminal for all 40 companies.

## FSSP reconciliation

For INN `0274101890`, a preserved official exact-INN result dated 2026-09-17 contains 96 rows. The 2026-09-16 bridge snapshot reports 74 current plus 22 completed, which also equals 96. This explains the aggregate count difference only as an `INFERENCE`. The official visible amount covers 85/96 rows and is not comparable with bridge total/remaining amounts; row identity and amount equivalence remain `UNVERIFIED`. Positive bridge evidence is allowed; negative bridge evidence does not close FSSP.

## Browser and performance

- Chromium 153 desktop and mobile: 40 cards, three score fields per card, reasons, limitations and evidence tables; exact-INN search 1/40; each group filter 10; no horizontal overflow; zero console errors.
- Read profile: 62–69 SQL queries and zero external calls, equal to the accepted query-count envelope. Current p50 is 1035.026 ms and p95 is 1909.876 ms versus 1012.274/1578.178 ms at the previous checkpoint. The p95 regression is recorded, not hidden; it followed the Alembic cold path and larger immutable local acceptance history, without query-count or network regression.

## Final gate

Stage 1.6 audit closure and runtime hardening are complete, but Product Recovery is **not accepted** because mandatory source coverage and the positive gate still fail. Company Card v2 and later phases remain blocked. Do not weaken thresholds, infer absence from blocked sources, or start the next stage.

Primary evidence: `STAGE_1_6_AUDIT_CLOSURE.md` and `artifacts/product_recovery_final/`.

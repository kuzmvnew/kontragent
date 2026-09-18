# Product Recovery v3 — final Stage 1.6 acceptance report

Date: 2026-09-18

Decision: **NOT ACCEPTED — EXTERNAL RUNTIME BLOCKERS REMAIN**

Code checkpoint: `d92ef5a82bb42506d7b31f802f92ec64b8d8fa24`

## What passed

- Canonical 15-capability catalog and four applicability classes are implemented and tested.
- Firmoteka remains an `AUTHORIZED_BRIDGE`; direct/dataset evidence has precedence and one capability contributes risk only once.
- Existing ERKNM and applicable licence/SRO results now enter v3 normalization.
- Bankinform without bank/account context is `NOT_APPLICABLE`, not `UNAVAILABLE`; positive cached facts are preserved.
- RNP is `DEFERRED_EXTERNAL_ACCESS`, never a fabricated universal N/A.
- Risk, evidence coverage and workflow completion remain separate.
- Positive wording requires coverage ≥90, all mandatory hard checks, risk <20 and no confirmed adverse risk point.
- Public GET remains DB/cache-only with zero writes and zero external calls; protected routes fail closed with 401.
- Read fan-out fell from 75–78 to 62–69 queries on the same five-company profile.
- Full regression: **655 passed**; Alembic: `b8c9d0e1f2a3 (head)`.

## Golden-40

| Metric | Result |
|---|---:|
| Companies | 40/40 |
| Workflow completion | 100–100% |
| Coverage | 51–78/100 |
| Average coverage | 63.5/100 |
| Positive conclusions | 0/40 |
| Low-risk candidates passing positive gate | 0/10 |
| Persisted immutable v3 pairs | 40 |
| ERKNM terminal evidence | 40/40 (1 found, 39 not-found) |

The same cohort was used. The historical UI bucket label was corrected from “Высокая полнота” to “Низкий наблюдаемый риск — кандидат” because observed coverage is only 51–54 in that bucket.

## Unresolved runtime

| Capability | Unresolved | Reason |
|---|---:|---|
| Arbitration | 40 | Checko credential absent; official KAD machine path unconfirmed |
| General courts | 40 | Regional/targeted access remains partial and timed out/challenged |
| CBR ZSK | 40 | SmartCaptcha; no completed authorized session |
| FSSP | 24 | Official exact-INN flow returned CAPTCHA; negative bridge cannot close |
| Bankruptcy / EFRSB | 21 | QRATOR/CAPTCHA and no authorized transport |
| Licences/SRO | 7 applicable companies | Required profile check remains unavailable |

RNP is reported separately as deferred external access and excluded from current coverage when unavailable. Bankinform is excluded only because the Golden cohort has no supplied bank/account/BIK context.

## UX and browser acceptance

- 40 cards render.
- Search returns 1/40 for an exact INN.
- Low-risk candidate filter returns 10/40.
- Limitations are grouped to at most four lines per card.
- Recommendations name the next official check.
- No `UNAVAILABLE`, `NOT_FOUND`, `PARTIAL`, `dataset_code` or `rule_code` leaks appear in the first view.
- Risk arithmetic, money formatting and evidence details remain available.

## Final gate

The audit/hardening stage is closed, but Product Recovery is not accepted. The next action is to wait for or establish authorized source access, then rerun the same Golden-40 cohort. Company Card v2 and subsequent phases remain blocked.

Primary evidence: `STAGE_1_6_AUDIT_CLOSURE.md` and `artifacts/stage_1_6/final_acceptance_summary.json`.

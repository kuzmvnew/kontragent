# Risk Engine Acceptance

Date/environment: 2026-09-17, user Mac, local branch `codex/risk-engine`, PostgreSQL database `kontragent`.  
Status: **IN PROGRESS — local Six Gates evidence recorded; GitHub CI/PR not yet recorded.**

Evidence classes follow `EVIDENCE_REPORTING_POLICY.md`.

## Baseline and implementation evidence

| Claim | Evidence Class | Artifact / command | Observed result | Limitations / not checked |
|---|---|---|---|---|
| Starting Git baseline | VERIFIED_FROM_GIT | `git rev-parse HEAD`; `git rev-parse origin/main` after fetch/ff-only | both `c131186ce2255d786c03f4c0c52e19d5723b273e` | Proves repository refs, not runtime |
| Starting migration | VERIFIED_RUNTIME | `uv run alembic current` | `d4e5f6a7b8c9 (head)` | Before Risk migration |
| Starting tests | VERIFIED_RUNTIME | `uv run python -m pytest tests -q` | `556 passed in 1.03s` | Browser/DB acceptance separate |
| Contracts/rules/engine/UI present | VERIFIED_FROM_GIT | `app/contracts/risk.py`, `app/risk_rules/v1.json`, `app/services/risk_engine_service.py`, `templates/partials/risk_engine.html` | v1 artifacts in working branch | GitHub PR/CI pending at this checkpoint |

## Current-run gates

| Gate | Evidence Class | Artifact / command | Observed result | Limitations / not checked |
|---|---|---|---|---|
| Automated semantics | VERIFIED_RUNTIME | `uv run python -m pytest tests/test_risk_engine.py -q`; full `uv run python -m pytest tests -q` | Risk tests `30 passed in 0.48s`; final local full regression `586 passed in 1.17s` | GitHub CI is a separate gate |
| Migration round trip | VERIFIED_RUNTIME | `uv run alembic upgrade head`; `downgrade d4e5f6a7b8c9`; `upgrade head`; `current` after each transition | `d4e5f6a7b8c9 -> e5f6a7b8c9d0 -> d4e5f6a7b8c9 -> e5f6a7b8c9d0 (head)` | Local PostgreSQL only; no production migration |
| Real data inputs | VERIFIED_RUNTIME | read-only SQL selection plus `recalculate_company_risk` | five real INNs evaluated: tax `6612045859`; court `1215214540`; partial/limitations `6320002223`; CBR warning `9309029650`; low observed signals `9727092307` | Does not claim these five represent the national population |
| PostgreSQL save/readback | VERIFIED_RUNTIME | independent SQL query on `company_risk_assessments` joined to `companies` | four queried assessment UUIDs reread with `risk-engine-1.0.0`, `risk-rules-1.0.0`, 15/16 signals and stored completeness; fifth low-signal UUID `e1f36dd2-cec6-4e99-a5e5-355a376cb379` saved and reused | PostgreSQL on user Mac only |
| Cache/recalculation | VERIFIED_RUNTIME | repeated `calculate_company_risk('9727092307')` and DealContext calculation | identical input reused UUID `e1f36dd2-cec6-4e99-a5e5-355a376cb379`; procurement context created UUID `f0b0d7d0-7522-490c-8331-3d99795cc63e`, `DEAL_CONTEXT_CHANGE` | Ruleset/source invalidation are automated-test evidence, not separately mutated in live DB |
| Browser/UI | VERIFIED_RUNTIME | local uvicorn `127.0.0.1:8766`; in-app Chromium; server access log; browser console log | four cards returned HTTP 200 (`1215214540`, `6320002223`, `9727092307`, `9309029650`); Risk UI rendered sections/completeness; expanded tax signal showed debt `2,228,577,247.95 RUB`, revenue `794,838,000 RUB`, ratio `280.38%`, source/coverage/freshness and rule v1.0.0; browser recalculation returned POST 303 then GET 200 and changed `calculated_at`; browser error/warn log `[]` | Desktop Chromium only; responsive/mobile not checked; Company Card v2 not claimed |
| Coverage/freshness/counts/dates | VERIFIED_RUNTIME | PostgreSQL readback, real result payloads, expanded Chromium signal | assessment payloads retain source dates, checked/calculated time, per-signal coverage/freshness and completeness; AVTOVAZ assessment stored `partial=1`, `unavailable=2`; low-signal assessment remained `NO_MATERIAL_SIGNALS` while `not_checked=6`, `unavailable=1` stayed visible | Completeness is not risk; no claim that unavailable sources are clean |

## Golden-company observations

- `6612045859`: tax debt signal `CONFIRMED_RISK`; saved assessment `9d38264e-617c-4387-b19b-ac2da7fda0a0`.
- `1215214540`: court activity plus tax signals; 7/7 cached arbitration cases; claims explicitly not debt; saved assessment `2c6c3edd-8dfa-41a7-83f3-48db5272d83c`.
- `6320002223`: targeted general-court partial coverage and address context; saved assessment `fbd75da0-eedc-48af-8196-3d34df428e58`.
- `9309029650`: exact CBR warning-list match created a confirmed compliance signal; saved assessment `53b5c5e5-8829-4ef4-877c-4df29793c3bd`.
- `9727092307`: no observed material signal in completed checks; overall `NO_MATERIAL_SIGNALS`, while six not-checked and one unavailable check remained separately visible.

## Semantics checked

`VERIFIED_RUNTIME` automated cases cover found risk, warning, info, no-risk-found, not-checked, unavailable, not-applicable, stale, partial coverage, multiple risks, versions, cache reuse/invalidation, recalculation, court partial sample and claim/debt distinction, ZSK found/negative/unavailable, account suspension/stale protected check, source-blocked, DealContext access-pending, mass-address context, identity uncertainty, liquidation/bankruptcy separation and dataset stale impact.

## External historical evidence

`EXTERNAL_HISTORICAL_EVIDENCE`: Stage 1.5 and Data Readiness acceptance documents retain their prior Six Gates/source acceptance. They informed boundaries but were not treated as current Risk Engine runtime evidence.

## Unverified / not checked

- GitHub PR and GitHub Actions: UNVERIFIED at this checkpoint.
- Production deployment and scheduler: NOT CHECKED; scheduler remains `NOT_CONFIGURED` where recorded.
- Production database/browser: NOT CHECKED.
- Massive scoring of 6.7m companies: NOT PERFORMED by design.
- New external sources, paid providers, CAPTCHA bypass and Summary Engine: NOT PERFORMED.
- Final stage status remains `IN PROGRESS` until the final repeat regression, clean diff, PR and green CI are recorded.

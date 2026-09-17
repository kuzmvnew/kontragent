# Summary Engine Acceptance

Date/environment: 2026-09-17, user Mac, local branch `codex/summary-engine`, PostgreSQL database `kontragent`.  
Status: **SUMMARY ENGINE IN PROGRESS**

Evidence classes follow `EVIDENCE_REPORTING_POLICY.md`.

## Baseline

| Claim | Evidence Class | Artifact / command | Observed result | Limitations / not checked |
|---|---|---|---|---|
| Starting Git baseline | VERIFIED_FROM_GIT | `git fetch origin`; `git switch main`; `git merge --ff-only origin/main`; `git rev-parse HEAD`; `git rev-parse origin/main` | local and origin `510fa97792be43a4eb561288d2a2e78cd8764d79`; clean worktree | Repository refs, not runtime |
| Starting migration | VERIFIED_RUNTIME | `uv run alembic current` | `e5f6a7b8c9d0 (head)` | Before Summary migration |
| Starting Risk tests | VERIFIED_RUNTIME | `uv run python -m pytest tests/test_risk_engine.py -q` | `30 passed in 0.47s` | Summary not yet implemented |
| Starting regression | VERIFIED_RUNTIME | `uv run python -m pytest tests -q` | `586 passed in 1.10s` | Browser/DB gates separate |

## Current-run gates

| Gate | Evidence Class | Artifact / command | Observed result | Limitations / not checked |
|---|---|---|---|---|
| Architecture and boundary | VERIFIED_FROM_GIT | `app/contracts/summary.py`, `app/services/summary_engine_service.py`, `app/models/summary.py`, `SUMMARY_ENGINE_ARCHITECTURE.md` | Deterministic projection from saved Risk assessment; no provider/raw-page/LLM path | No external AI wording |
| Automated semantics | VERIFIED_RUNTIME | `.venv/bin/python -m pytest tests/test_summary_engine.py -q` | `23 passed`; claim/debt, ZSK, unavailable, not-checked, not-applicable, partial, denominator, numeric wording, ordering, traceability, modes, privacy, versioning and cache covered | Final full-suite result recorded below |
| Migration round trip | VERIFIED_RUNTIME | `uv run alembic upgrade head`; `downgrade e5f6a7b8c9d0`; `upgrade head`; `current` after transitions | `e5f6a7b8c9d0 -> f6a7b8c9d0e1 -> e5f6a7b8c9d0 -> f6a7b8c9d0e1 (head)` | Local PostgreSQL only; no production migration |
| Real persisted assessments | VERIFIED_RUNTIME | `generate_company_summary` over five existing Risk assessment rows | tax `6612045859`; court `1215214540`; partial `6320002223`; regulatory `9309029650`; low observed/incomplete `9727092307` | Five golden cases are not a population claim |
| PostgreSQL save/readback | VERIFIED_RUNTIME | SQL join of `company_summaries` to `companies`; service readback | five USER v1.0.1 summaries saved with Risk/Rules versions and explainability refs; repeated generation reused the same summary id | User Mac database only |
| Browser/UI | VERIFIED_RUNTIME | local uvicorn `127.0.0.1:8766`; in-app Chromium; server access log; browser console log | five real cards HTTP 200; Summary visible with conclusions, factors, limitations, actions, dates/versions and “Почему” links; POST returned 303 then GET 200; console error/warn `[]` | Desktop Chromium only; responsive/mobile not checked; Company Card v2 not claimed |
| Counts/dates/versions/traceability | VERIFIED_RUNTIME | Chromium DOM plus PostgreSQL structured payload/readback | UI showed `summary-engine-1.0.1`, `risk-engine-1.0.0`, `risk-rules-1.0.0`; tax values/date/ratio, court claimed amount/caveat and completeness counts visible; statements link summary/risk ids, rules, evidence and dates | Some persisted Risk signals do not contain every possible denominator/period; Summary states the missing metric rather than inventing it |
| GitHub PR and CI | UNVERIFIED | PR/CI not yet recorded in this draft | Pending final commit/push/PR | Must be updated from GitHub evidence before handoff |

## Golden-company observations

- `6612045859`: tax debt `15,702,961,090.63 RUB` on `2026-08-01`, revenue `23,912,000 RUB`, ratio `65,669.79%`; incomplete checks remain visible.
- `1215214540`: tax amount/ratio plus 7/7 arbitration cases and `2,778,964.84 RUB` claimed amount; wording says claims are not confirmed debt and that saved claims/revenue materiality lacks a valid denominator.
- `6320002223`: general-court factor and limitation both preserve partial regional coverage.
- `9309029650`: exact-identifier Bank of Russia warning-list factor uses narrow regulatory wording, not a reliability verdict.
- `9727092307`: no observed material signal in completed checks while eight incomplete/partial/unavailable checks remain explicit; no “company safe” wording.

## External historical evidence

`EXTERNAL_HISTORICAL_EVIDENCE`: Risk Engine, Stage 1.5 and Data Readiness acceptance documents retain their dated results. They establish upstream boundaries but do not substitute for the Summary Engine runtime evidence above.

## Unverified / not checked

- Production deployment/database/browser: NOT CHECKED.
- Legal approval for PUBLIC projection and SEO: NOT PERFORMED; `public_projection_approved=false`.
- Report v1/PDF, Company Card v2, Bulk product and Person Core: NOT IMPLEMENTED by this stage.
- Mass generation for all companies: NOT PERFORMED by design.
- Responsive/mobile browser: NOT CHECKED.

## Final decision

The final decision is conditional on replacing the pending PR/CI row with current GitHub evidence and completing the final full regression. No merge is performed automatically.

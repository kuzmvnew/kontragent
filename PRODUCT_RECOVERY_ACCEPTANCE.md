# Product Recovery acceptance

Date: 2026-09-17
Decision: **PRODUCT RECOVERY IN PROGRESS**

## Verified in this run

- `VERIFIED_FROM_GIT`: recovery branch created from `c8d4fc79d856a350422583f4c46e4ed4bacef279`.
- `VERIFIED_RUNTIME`: baseline Alembic `f6a7b8c9d0e1`; baseline `609 passed` before recovery edits.
- `VERIFIED_RUNTIME`: database counts are recorded in `PRODUCT_RECOVERY_SOURCE_STATUS.md`.
- `VERIFIED_RUNTIME`: post-change suite `621 passed`; `git diff --check` is clean.
- `VERIFIED_RUNTIME`: Alembic is at `f6a7b8c9d0e1 (head)`; this recovery increment adds no migration.
- `VERIFIED_RUNTIME`: AО «АВТОВАЗ» (`6320002223`) recalculates as `INSUFFICIENT_DATA`; the partial regional general-court result has severity `NONE`, and five unfinished mandatory checks prevent a clean conclusion.
- `VERIFIED_RUNTIME`: the АВТОВАЗ card rendered successfully in headless Chromium at 1440×1200 and 390×844; both screenshots were inspected. This is one-card smoke coverage, not the required 40-card browser acceptance.
- `VERIFIED_RUNTIME`: visible-text scan of that card found none of the checked internal/English tokens (`NOT_CHECKED`, `UNAVAILABLE`, `PARTIAL_COVERAGE`, `ACCESS_PENDING`, `HUMAN_ACTION_REQUIRED`, `API key`, `exact ИНН`, `RUB`).
- `VERIFIED_FROM_GIT`: Risk/Summary v2 and orchestrator foundation are present with regression tests.

## Acceptance matrix

| Gate | Current result |
|---|---|
| Master registry enriched | BLOCKED — official full EGRUL/EGRIP files/access absent |
| 10 active bankruptcy companies | BLOCKED — Fedresurs production source absent; DB count 0 |
| FSSP working flow | IN PROGRESS — fail-closed human-action foundation exists; no completed company results |
| FULL orchestrator | IMPLEMENTED FOUNDATION — live source acceptance pending |
| Risk v2 | IMPLEMENTED FOUNDATION — 621-test suite green; 40-company evidence pending |
| Summary v2 | IMPLEMENTED FOUNDATION — one-card desktop/mobile smoke passed; 40-company evidence pending |
| Exactly 40 live companies | NOT STARTED; source prerequisites unmet |
| Browser acceptance 40/40 | 1/40 smoke only; acceptance not started |
| Full suite / CI | LOCAL PASS (621); remote CI not run |

No `COMPLETE`, `PASS`, or product-accepted claim is made.

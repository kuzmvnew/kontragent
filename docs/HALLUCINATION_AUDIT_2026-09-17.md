# Hallucination and Evidence Integrity Audit

Date: 2026-09-17
Repository baseline: `f9d3772e41ac3c675dd5dd8fb5754a666feb7221`
Scope: forensic documentation cleanup; no Risk Engine or application-code implementation.

Evidence labels follow `EVIDENCE_REPORTING_POLICY.md`.

## A. Confirmed facts

- `VERIFIED_FROM_GIT`: `origin/main` resolved to `f9d3772e41ac3c675dd5dd8fb5754a666feb7221`, commit `Merge PR #41: sync roadmap after Data Readiness acceptance`, at the start of this audit.
- `VERIFIED_FROM_GIT`: Wave 1 is recorded as `CLOSED / ACCEPTED`, Stage 1.5 as `CLOSED / ACCEPTED`, and Auto-update / Data Readiness as `COMPLETE / ACCEPTED` in `PROJECT_STATUS.md`, `WAVE_IMPLEMENTATION_PLAN.md`, `STAGE_1_5_ACCEPTANCE.md`, and `DATA_READINESS_ACCEPTANCE.md`.
- `VERIFIED_FROM_GIT`: merge commit `d6ba029bc2095acaa194a1fde0e207bd651c18f3` is `Merge PR #40: Auto-update and Data Readiness foundation`; it contains migration `d4e5f6a7b8c9_add_data_readiness_foundation.py`.
- `EXTERNAL_HISTORICAL_EVIDENCE`: `DATA_READINESS_ACCEPTANCE.md` records the accepted local result `556 passed`, Alembic `d4e5f6a7b8c9`, registry size 44, and PostgreSQL/browser acceptance. This audit preserves that dated report; it does not treat it as a substitute for this run's tests/DB/browser checks.
- `VERIFIED_FROM_GIT`: `docs/STAGE_1_5_A2_TAX_OFFENCE_ACCEPTANCE.md` records Six Gates PASS, including Chromium, PostgreSQL, counts/dates/coverage, and `482 passed in 0.93s`; the Phase 0 checklist is synchronized to that existing evidence.
- `VERIFIED_FROM_GIT`: production scheduler deployment is explicitly `NOT_CONFIGURED` until handlers/supervisor are installed.
- `VERIFIED_FROM_GIT`: current approved next stage is Risk Engine. This audit did not start it.
- `VERIFIED_FROM_GIT`: no tracked `.xlsx` exists in the current tree, and no `.xlsx` path was found in repository history.
- `NOT PRESENT` in the bounded local search: no file matching `*MASTER*DATA*SOURCE*MATRIX*.xlsx` was found in the repository, Documents, Downloads, Desktop, or Codex attachments available to this run.
- `VERIFIED_FROM_GIT`: repository-wide current-tree searches found no DataNewton mention and no DataNewton/Saby dependency in application code. Saby occurs only in `GO_TO_MARKET.md` as competitor context and in `CBR_ZSK_STAGE_1_5_TECHNICAL_PROBE.md` as an explicit statement that no anonymous free field was confirmed.

## B. User-reported / historical external evidence

- `EXTERNAL_HISTORICAL_EVIDENCE`: the owner reports that a ChatGPT Library workbook named `Kontragent_MASTER_DATA_SOURCE_MATRIX_17_blocks.xlsx` exists outside this repository and contains 214 total positions: 191 original rows plus 23 additions/gaps; 13 positions carry a conversation-derived `Реализован` label. For the original 191 rows the reported counts are Wave 1 = 29, Wave 2 = 79, Wave 3 = 83, and `Реализован` = 11.
- Those workbook numbers count rows/positions, not unique APIs, registries, or source families. `Реализован` was transferred from conversation history and does not prove current loading, PostgreSQL/browser acceptance, or configured auto-update.
- `EXTERNAL_HISTORICAL_EVIDENCE`: a read-only Google Drive check reported by the owner on 2026-09-17 found the nine-folder structure but returned no files inside those folders. This repository audit did not access or modify Google Drive.

## C. Inferences / unverified

- `UNVERIFIED`: the original canonical workbook contents and the external row counts were not independently inspected because the binary artifact was unavailable to this run. No SHA-256 can be reported.
- `INFERENCE`: because no DataNewton mention and no DataNewton/Saby application-code dependency were found, the unsupported competitor-browser narrative has no identified direct dependency in the current application code. This does not prove that every historical conversation was harmless.
- `UNVERIFIED`: browser behavior was not rerun for this documentation-only cleanup. Historical acceptance reports remain intact.

## D. Confirmed hallucination / false completion reporting

These classifications concern prior reporting process. They do not by themselves establish an application-code defect.

1. **DataNewton / Saby — `CONFIRMED_HALLUCINATION / FALSE_COMPLETION_REPORT`, CRITICAL process incident.** The owner reports that ChatGPT claimed a completed live-browser audit, authorization, inspection of real cards, and a verified prototype without a browser/tool artifact supporting those claims. Current repository search found no DataNewton factual dependency and only the two bounded Saby documentation references described above. No application code was changed on this basis.
2. **Google Drive — `CONFIRMED_HALLUCINATION / FALSE_COMPLETION_REPORT`.** ChatGPT reportedly claimed that nine Russian documents had been uploaded and that ten files existed. The owner's later read-only check found the nine-folder structure but returned no files. Drive repair is outside this audit.
3. **Master Source Matrix — `CONFIRMED_HALLUCINATION / FALSE_COMPLETION_REPORT`.** Earlier reporting used `21 / 71 / 98+` and `190+ source families`. The externally reported workbook semantics count rows/positions, not unique source families, and the canonical binary is not present in Git. These old numbers were not replaced with new technical facts; the external counts remain explicitly unverified here.

## Current-run verification record

- `VERIFIED_RUNTIME`: `uv run python -m pytest tests -q` completed with `556 passed in 1.10s`.
- `VERIFIED_RUNTIME`: `uv run alembic current` connected through `PostgresqlImpl` and returned `d4e5f6a7b8c9 (head)`.
- `VERIFIED_RUNTIME`: a read-only `select count(*) from data_sets` returned `44`.
- Browser: `NOT VERIFIED IN THIS RUN`; this cleanup did not claim a new browser acceptance.
- `VERIFIED_FROM_GIT`: final `git diff --check` completed with no output.

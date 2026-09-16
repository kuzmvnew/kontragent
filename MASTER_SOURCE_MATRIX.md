# MASTER SOURCE MATRIX — Kontragent

Status: APPROVED RESEARCH SNAPSHOT
Date: 2026-09-17

The canonical spreadsheet artifact for the researched source matrix is:

`Kontragent_MASTER_DATA_SOURCE_MATRIX_17_blocks.xlsx`

It contains:

- 17 researched source blocks;
- consolidated source table;
- wave/status data;
- access/cost/machine-readable notes;
- derived metrics and client meaning;
- explicit gaps where the research did not confirm a source/access path.

## Development sequence

1. Complete Wave 1. W1-006 is the next executable step while W1-005 remains externally source-blocked B/C.
2. After Wave 1, follow `POST_WAVE1_PRODUCT_PLAN.md`.
3. Build Auto-update/Data Readiness, Company Card v2, Risk/Summary and Report.
4. Pass Security & Resilience, Legal Launch and Product / Company Card Acceptance gates.
5. Release the first 10,000 company pages for SEO.
6. Build Lists/Bulk Check, Monitoring/Event Engine and Workspace v1.
7. Start Wave 2 only after that Company B2B Core stage.

See `WAVE_IMPLEMENTATION_PLAN.md` for the active order of work.

## Source implementation rule

Before any new Wave 2 source is coded, it must pass `SOURCE_ACCESS_COST_GATE.md` and answer a concrete workflow need: company card, risk, monitoring, bulk check, Person, Compliance or a justified sector/context check.

Do not treat public browser access as automatic permission for bulk collection, storage, commercial use or republication.

Transport sources are expanded in `TRANSPORT_SOURCE_AUDIT.md`; each concrete registry still requires its own source passport/access decision before implementation.

## Current integration inventory caution

Before planning FSSP or Fedresurs as “new” Wave 2 integrations, re-audit current `main`, historical branches and the user’s earlier/local project state. Current `main` inspection on 2026-09-17 did not confirm dedicated implementation paths for those sources, but the user has indicated they may exist in prior/local work. Do not duplicate verified working integrations.

Project name remains **Kontragent**. “Next” module/product names are working naming ideas only and are not an approved rename.

## Binary workbook storage

The `.xlsx` workbook itself must be committed as a binary repository file (recommended path: `docs/research/Kontragent_MASTER_DATA_SOURCE_MATRIX_17_blocks.xlsx`). The current text documentation index does not replace the binary workbook with a partial CSV export.

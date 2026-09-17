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

1. Wave 1 is officially CLOSED / ACCEPTED. See `WAVE1_CLOSURE_DECISION.md` and `WAVE1_STATUS.md`.
2. Execute `INTERMEDIATE_STAGE_1_5.md` before the full Risk Engine.
3. Stage 1.5 closes unfinished acceptance of FNS Tax Debt/Tax Offences and adds/recovers critical free-source foundations: FSSP, Fedresurs/EFРSB, courts, FNS account-suspension decisions, Bank of Russia high-risk public check and corporate disclosure.
4. Build Auto-update/Data Readiness.
5. Build Risk Engine -> Summary Engine -> Company Card v2 -> Report v1.
6. Pass Security & Resilience, Legal Launch and Product / Company Card Acceptance gates.
7. Release the first 10,000 company pages for SEO.
8. Build Lists/Bulk Check, Monitoring/Event Engine and Workspace v1.
9. Start Wave 2 only after that Company B2B Core stage.

See `WAVE_IMPLEMENTATION_PLAN.md` for the active order of work.

## Stage 1.5 source class

Stage 1.5 is not Wave 2. It is a prerequisite data-quality/product-foundation stage required before the agreed Risk Engine.

Current Stage 1.5 source/work items:

- FNS Tax Debt — final six-gate acceptance;
- FNS Tax Offences — final six-gate acceptance;
- FSSP — inventory/recover existing implementation first;
- Fedresurs/EFРSB — inventory/recover existing implementation first;
- Arbitration Courts v1;
- Courts of General Jurisdiction v1;
- bankruptcy/liquidation event model;
- FNS bank-account suspension decisions;
- Bank of Russia KYC/high-risk public-check technical probe;
- free-first corporate disclosure;
- Company Card contact/address/bank/relationship facts needed by later Risk/Summary.

Detailed scope: `INTERMEDIATE_STAGE_1_5.md`.

## Source implementation rule

Free/public/official sources are preferred.

Before any later Wave 2 source is coded, it must pass `SOURCE_ACCESS_COST_GATE.md` and answer a concrete workflow need: company card, risk, monitoring, bulk check, Person, Compliance or a justified sector/context check.

A paid source is not the default. It requires a separate explicit commercial decision after free options are evaluated.

Do not treat public browser access as automatic permission for unlimited bulk collection, storage, commercial use or republication. For public live sources use a targeted, low-load, cache-first mode unless an approved bulk interface exists.

Transport sources are expanded in `TRANSPORT_SOURCE_AUDIT.md`; each concrete registry still requires its own source passport/access decision before implementation.

## Corporate disclosure

Corporate disclosure is now a Stage 1.5 free-first source family rather than a paid-feed dependency.

Plan: `CORPORATE_DISCLOSURE_SOURCE_PLAN.md`.

Target facts include, when actually disclosed:

- corporate/significant messages;
- financial/interim reports;
- affiliated persons;
- registrar;
- public contacts/addresses;
- publicly disclosed bank details;
- other monitoring/risk-relevant corporate events.

Paid disclosure APIs/FTP are optional later decisions, not a Stage 1.5 requirement.

## FSSP / Fedresurs inventory caution

Before planning FSSP or Fedresurs as new integrations, re-audit current `main`, historical branches and the user’s earlier/local project state. The product owner has stated that these sources were developed previously.

Do not duplicate verified working integrations. Classify recovered work as `READY / PARTIAL / LEGACY_MIGRATION / NOT_FOUND` and then continue from evidence.

## Wave 2 boundary

Wave 2 remains later expansion of an already working Company B2B Core.

Typical Wave 2 groups include:

- movable-property pledges;
- Rospatent / trademarks / patents / IP;
- transport registries from `TRANSPORT_SOURCE_AUDIT.md`;
- additional professional/sector registries;
- additional licences/permissions;
- RNP/EIS when official access is available;
- other free official/public sources that close a concrete product workflow.

Project name remains **Kontragent**. “Next” module/product names are working naming ideas only and are not an approved rename.

## Binary workbook storage

The `.xlsx` workbook itself must be committed as a binary repository file (recommended path: `docs/research/Kontragent_MASTER_DATA_SOURCE_MATRIX_17_blocks.xlsx`). The current text documentation index does not replace the binary workbook with a partial CSV export.

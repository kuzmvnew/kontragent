# Fedresurs / EFRSB Stage 1.5 Inventory

Date: 2026-09-17

Classification: **INVENTORY COMPLETE / LEGACY IMPLEMENTATION NOT FOUND**

## Decision

No working or reusable Fedresurs/Unified Federal Register of Bankruptcy Information (EFRSB) implementation was found. The project contains only disabled source-catalog metadata (`fedresurs` / `fedresurs_messages`) and planning documents. Neither is an integration and neither proves source access, stored events, product visibility, or update readiness.

This completes the mandatory recovery search before new development. No bankruptcy or liquidation conclusion may be synthesized from the placeholder.

## Evidence reviewed

### Current repository

- `app/services/source_service.py` registers disabled `fedresurs` and `fedresurs_messages` catalog entries.
- The dataset is described as generic events with `update_mode=api`, `data_format=json`, and no `source_url`.
- No Fedresurs/EFRSB model, migration, provider, ingestion service, parser, test, API projection, template, or UI block exists.
- The only other implementation references are generic bankruptcy-domain comments and unrelated CBR liquidation-status fields.
- `FINANCIAL_DISTRESS_SPEC.md` is a specification, not source code or accepted data.

### Git history and branches

- All visible local and remote branches, commit subjects, filenames, reflog entries, and unreachable commits were searched.
- The only matching historical commit adds the financial-distress specification; it contains no Fedresurs/EFRSB implementation.
- No restorable provider, schema, parser, data artifact, or product block was found.

### Local project copies

- `/Users/mikhailkuznetsov/Documents/kontragent_backup_before_postgres` was searched by filename and content.
- It contains no Fedresurs/EFRSB/bankruptcy implementation.

### PostgreSQL

- No public table name contains `fedresurs`, `bankrupt`, or `liquidat`.
- Catalog row: source `fedresurs` disabled; dataset `fedresurs_messages` disabled; domain `events`; `source_url` null; update mode `api`; format `json`.
- Ingestion runs for `fedresurs_messages`: **0**; no first/last run timestamps exist.

## Six-gate status

The six acceptance gates do not pass because there is no source implementation to accept:

| Gate | Result | Evidence |
|---|---|---|
| Automated tests | NOT CONFIRMED | no Fedresurs/EFRSB tests or implementation |
| Real official/public result | NOT CONFIRMED | no provider/source call |
| PostgreSQL write/read | NOT CONFIRMED | no relevant tables; zero runs |
| State semantics | NOT CONFIRMED | no `found/not_found/not_applicable/unavailable` contract |
| Chromium/product verification | NOT CONFIRMED | no product projection |
| Counts/dates/coverage | NOT CONFIRMED | no accepted dataset |

## Semantic guardrails for later work

- Bankruptcy, liquidation, intention notices, creditor notices, and other significant facts are distinct event types and must remain distinct.
- Liquidation is not bankruptcy.
- A notice or claim is not itself proof of a final legal outcome or confirmed debt.
- Preserve publication date, event date when supplied, publisher, message identifier, original event type, source URL/evidence, and company matching basis.
- Source/network/auth failures must produce `unavailable`, never `not_found`.
- A dated negative result may be emitted only within demonstrated source coverage.
- Research and approve a free/public/official route first; a paid dependency requires an explicit product-owner decision.

## Outcome

Inventory/recovery decision is complete. There is nothing safe to migrate or accept. Any future Fedresurs/EFRSB work is a new source integration and requires an access/cost decision, source passport, explicit bankruptcy/liquidation semantics, and full six-gate acceptance.

Backlog: **FEDRESURS NEW IMPLEMENTATION / FREE-FIRST SOURCE RESEARCH** in the next data-enrichment planning cycle. The accepted D event model remains unchanged.

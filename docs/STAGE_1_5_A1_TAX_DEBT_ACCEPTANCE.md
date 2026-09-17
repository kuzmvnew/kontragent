# Stage 1.5 A1 — FNS Tax Debt Acceptance

Date: 2026-09-17
Status: **ACCEPTED / SIX GATES PASS**
Source passport: `FNS_TAX_DEBT_SOURCE_PASSPORT.md`

## Scope

This acceptance closes the existing FNS Tax Debt implementation. It does not introduce a replacement source, a paid dependency, auto-update scheduling, Risk Engine logic or a Company Card v2 redesign.

## Six Gates

| Gate | Status | Evidence |
|---|---|---|
| 1. Automated tests | PASS | New parser fail-closed/component tests plus existing check-contract tests; full regression recorded below. |
| 2. Real official source | PASS | Official FNS dataset `7707329152-debtam`; ZIP `data-20260825-structure-20181201.zip`; SHA-256 `8f39aee65b09c716d0b3e5900ecf9efb8938a064b1f1eb491ea309385a8cda65`; ZIP CRC PASS; 1,052 XML files; source data date 2026-08-01; document date 2026-08-25. |
| 3. PostgreSQL write/readback | PASS | Successful ingestion run reread; 625,395 current snapshots and 2,031,361 items; real company service and product aggregator reread the stored rows. |
| 4. State semantics | PASS | Real `found`, real dated `not_found`, real IP `not_applicable`; automated `dataset_not_registered` and `dataset_not_loaded` => `unavailable`; invalid money/inconsistent source totals fail closed. |
| 5. Chromium | PASS | Real local cards for INN `6612045859` (`found`), `9705252500` (`not_found`) and `784305462631` (`not_applicable`); all HTTP pages rendered, required Tax Debt text visible, browser error log empty. |
| 6. Counts/dates/coverage | PASS | Counts and dates below; all 625,395 snapshot totals equal their item sums; all 2,031,361 item totals equal arrears + penalties + fines. |

## Real cases

### Found

- INN: `6612045859`.
- Company: ООО "УРАЛЦВЕТЛИТ".
- Result: `found` / `has_debt=true`.
- Total: 15,702,961,090.63 RUB.
- Arrears: 6,939,403,266.18 RUB.
- Penalties: 8,527,533,150.45 RUB.
- Fines: 236,024,674.00 RUB.
- Item rows: 4.
- Data date: 2026-08-01.
- Chromium showed the same totals, itemisation and date.

### Dated not_found

- INN: `9705252500`.
- Company: ООО "ОТЭКО".
- Result: `not_found` only within the successfully loaded FNS snapshot dated 2026-08-01.
- Chromium wording explicitly limits absence to the current published snapshot and date.

### Not applicable

- INN: `784305462631` (IP).
- Result: `not_applicable`, reason `legal_entities_only`.
- Chromium explicitly says the connected dataset is used for legal entities.

### Unavailable

Automated contract tests prove that an unregistered or not-loaded dataset returns `unavailable`, never `not_found`. The local accepted dataset was not deliberately disabled because that would mutate valid acceptance data merely to reproduce a failure state.

## Import and coverage

- Source XML documents read: 946,887.
- Valid: 946,887.
- Invalid: 0.
- Exact-INN Master matches/current snapshots: 625,395.
- Source documents unmatched to current Master: 321,492.
- Current detail items: 2,031,361.
- Master Registry: 6,781,487 entities.
- Legal entities in Master: 2,043,659.
- Matched snapshot coverage: 9.2221% of all Master entities; 30.6017% of legal entities.
- Snapshot/item aggregate mismatches: 0.
- Item component mismatches: 0.
- Current totals: arrears 877,097,848,849.27 RUB; penalties 349,666,484,616.75 RUB; fines 79,175,657,489.65 RUB; total 1,305,939,990,955.67 RUB.
- Stored history dates: 1 (`2026-08-01`). History storage/readback works, but no trend can be claimed from one accepted date.

`not_found` coverage means absence from this dated FNS publication, not absence from FNS internal systems and not a live no-debt certificate.

## Regression and schema

- Full regression after A1 changes: `477 passed in 1.03s`.
- Alembic: `e7a8b9c0d1e2 (head)`.
- PostgreSQL database: `kontragent`.
- No migration was required for A1.

## Source distinction

The accepted `debtam` facts are ordinary published tax debt split into arrears, penalties and fines. They are not evidence that threshold debt was transferred to a bailiff. That separate FNS fact has no confirmed current bulk path in this workstream and is neither inferred nor merged with FSSP.

## Commands

```text
uv run python -m pytest tests -q
PYTHONPATH=. uv run python scripts/accept_stage_1_5_tax_debt.py
uv run alembic current
```

Chromium evidence was collected from the running local application; screenshots were not committed.

## Limitations

- Manual snapshot ingestion; `auto_update=NOT_CONFIGURED`.
- One stored source date, so historical trend is not yet available.
- Data are dated publication facts and may not reflect later repayment.
- Dataset applies to legal entities only.
- No raw dataset export or public API entitlement is asserted here.
- The separate bailiff-referral threshold fact remains unimplemented/unconfirmed and is not synthesized from this dataset.

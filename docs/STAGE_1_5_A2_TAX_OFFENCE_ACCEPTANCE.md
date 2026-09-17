# Stage 1.5 A2 — FNS Tax Offences Acceptance

Date: 2026-09-17
Status: **ACCEPTED / SIX GATES PASS**
Source passport: `FNS_TAX_OFFENCE_SOURCE_PASSPORT.md`

## Scope

This acceptance closes the existing FNS Tax Offences implementation. It does not introduce a paid dependency, auto-update scheduling, Risk Engine logic or a Company Card v2 redesign.

## Six Gates

| Gate | Status | Evidence |
|---|---|---|
| 1. Automated tests | PASS | Parser amount/date/summing/fail-closed tests plus existing state-contract tests; full regression recorded below. |
| 2. Real official source | PASS | Official FNS dataset `7707329152-taxoffence`; ZIP `data-20251201-structure-20191201.zip`; SHA-256 `1a388022e0db361dc1cc78d65b4eb6f5f08a1d1fc59a9c6d035e1e8b4b4e384b`; ZIP CRC PASS; 111 XML files; data date 2024-12-31; XML document date 2025-12-02. |
| 3. PostgreSQL write/readback | PASS | Successful ingestion run reread; 24,139 current documents for 24,139 companies; service and product aggregator reread the stored rows. |
| 4. State semantics | PASS | Real `found`, real dated `not_found`, real IP `not_applicable`; automated missing/not-loaded dataset => `unavailable`; malformed money/documents fail closed. |
| 5. Chromium | PASS | Real local cards for INN `1215214540` (`found`), `9102309919` (`not_found`) and `784305462631` (`not_applicable`); required Tax Offences text visible; browser error log empty. |
| 6. Counts/dates/coverage | PASS | Counts, totals, exact-INN coverage and the one stored history date recorded below. |

## Real cases

### Found

- INN: `1215214540`.
- Company: ООО "СК "ЭВЕРЕСТ"".
- Result: `found` / `has_offence=true`.
- Fine amount: 241,486,695.40 RUB.
- Documents: 1.
- Document date: 2025-12-02.
- Data period/date: 2024-12-31.
- Chromium showed the same amount, document count and dates.

### Dated not_found

- INN: `9102309919`.
- Company: АО "КРЫМАВТОДОР".
- Result: `not_found` only within the successfully loaded FNS release dated 2024-12-31.
- Chromium wording explicitly limits absence to the current published dataset/period.

### Not applicable

- INN: `784305462631` (IP).
- Result: `not_applicable`, reason `legal_entities_only`.
- Chromium explicitly says the connected dataset contains legal-entity information.

### Unavailable

Automated contract tests prove that an unregistered or not-loaded dataset returns `unavailable`, never `not_found`. The accepted local dataset was not deliberately disabled merely to reproduce failure.

## Import and coverage

- Source XML documents read/valid: 100,005 / 100,005.
- Invalid: 0.
- Exact-INN matched/stored documents: 24,139.
- Matched companies: 24,139.
- Source documents unmatched to current Master: 75,866.
- Aggregate fine across the full source snapshot: 9,630,363,483.10 RUB.
- Aggregate fine stored for exact-INN Master matches: 2,751,655,839.40 RUB.
- Master Registry: 6,781,487 entities.
- Legal entities in Master: 2,043,659.
- Matched-company coverage: 0.3560% of all Master entities; 1.1812% of legal entities.
- Stored history dates: 1 (`2024-12-31`). History readback works, but no trend can be claimed from one accepted period.

`not_found` means absence from the limited dated FNS publication, not absence of every tax offence in all periods. The source publishes the aggregate fine, not the offence description; the UI retains that limitation.

## Regression and schema

- Full regression after A2 changes: `482 passed in 0.93s`.
- Alembic: `e7a8b9c0d1e2 (head)`.
- PostgreSQL database: `kontragent`.
- No migration was required for A2.

## Commands

```text
uv run python -m pytest tests -q
PYTHONPATH=. uv run python scripts/accept_stage_1_5_tax_offence.py
uv run alembic current
```

Chromium evidence was collected from the running local application; screenshots were not committed.

## Limitations

- Manual annual snapshot ingestion; `auto_update=NOT_CONFIGURED`.
- One stored source period, so historical trend is not yet available.
- The FNS publication has explicit period/cutoff criteria and is not a lifetime offence history.
- The source does not disclose the specific offence type.
- Dataset applies to legal entities only.
- No raw dataset export or public API entitlement is asserted here.

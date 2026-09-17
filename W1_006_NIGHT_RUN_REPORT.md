# W1-006 NOSTROY / SRO — night run report

Final state: **IMPLEMENTED / INTEGRATION PENDING**

Scope state: **RESEARCH COMPLETE / USER SCOPE REVIEW REQUIRED**

## Run

- start: 17.09.2026, approximately 06:52 MSK;
- finish: 17.09.2026, 07:12 MSK;
- duration: approximately 20 minutes;
- maximum window: 60 minutes, implementation frozen before final checks;
- baseline SHA: `eb5a4b9c364177ad9c7a6b7b8b3794d576b3057e`;
- actual `origin/main`: same SHA;
- baseline: `458 passed in 1.00s` (`real 1.60s`);
- baseline Alembic: `c5e1a2b3d4f5 (head)`;
- isolated Alembic after W1-006: `d6f7a8b9c0d1 (head)`.

Before work, `main` was clean but behind; it was fast-forwarded from
`911bc8d` to `eb5a4b9`, then branch `codex/w1-006` was created.

## Documents read

The required current status, roadmap, source matrix, post-Wave1 specs, three
launch gates, Wave1 handoff/acceptance documents and W1-005 passport were
reviewed. `README.md` does not exist. The XLSX referenced by the source matrix,
`Kontragent_MASTER_DATA_SOURCE_MATRIX_17_blocks.xlsx`, is absent and was not
recreated.

## Research result

Official candidates and comparison are recorded in
`NOSTROY_SRO_SOURCE_PASSPORT.md`. Selected conservative scope: only the
НОСТРОЙ construction-members registry. It excludes НОПРИЗ, НРС/person data and
the unsupported interpretation “all Russian SRO”. Confidence in technical
identity/response semantics is high; confidence in approved product scope and
commercial automation/reuse rights is insufficient. User and legal review are
required.

Official URLs:

- `https://reestr.nostroy.ru/sro/all/member/list`;
- `https://reestr.nostroy.ru/api/sro/all/member/list`;
- `https://nostroy.ru/dokumenty/reglament_er.pdf`.

Machine mode: undocumented JSON POST used by the official frontend; no key.
No published API documentation, SLA, rate limit, commercial-reuse permission
or general bulk export was found. The implementation is low-load on-demand
only and retains no person/contact fields.

## Real evidence, counts and dates

- official first page: HTTP 200, `count=419695`, 20 rows, 20,985 pages;
- `5907056036`: HTTP 200, found one historical membership, member `4047586`,
  ОГРН `1135907001801`, status `Исключен`;
- `9102309919`: HTTP 200, zero rows / not_found;
- request/cache date: 17.09.2026;
- record event dates remain separate (`2014-03-12`; last updated
  `01.08.2018 09:55:03` for the found sample);
- checksums are in the passport; raw responses remain temporary and are not
  committed;
- full registry completeness, duplicates/conflicts and archive integrity:
  NOT CONFIRMED because there is no approved bulk snapshot.

Exact identifiers: 10-digit company INN (matching), 13-digit OGRN
(corroboration), member id, member registration/inventory number, SRO id and
SRO registration number. Names/addresses/contacts are not matching evidence.

## Implementation and PostgreSQL

New provider validates HTTP, JSON shape, count, exact INN and OGRN, strips
director/address/phone, and maps transport/protection/schema/identity errors to
source errors. Service requires explicit context applicability, distinguishes
all four states and uses a per-INN/per-date cache.

Migration `d6f7a8b9c0d1` was applied to local PostgreSQL `kontragent`. Two real
rows were written and independently read:

- `5907056036`: success, found, 1 record, HTTP 200;
- `9102309919`: success, not_found, 0 records, HTTP 200.

No prior-source row was changed. Auto-update remains `NOT_CONFIGURED`.

## Six Gates

| Gate | Status | Evidence |
|---|---|---|
| 1 Tests | PASS | 7 focused tests; full regression `465 passed in 1.12s` (`real 1.77s`) |
| 2 Real official source | PASS | three low-load official API responses; two exact-INN product checks |
| 3 PostgreSQL write/read | PASS | migration head and independent reread of 2 cache rows |
| 4 State semantics | PASS | found/not_found/not_applicable/unavailable tests; errors never become absence |
| 5 Real Chromium | INTEGRATION PENDING | existing company card cannot be edited under night constraint |
| 6 Counts/dates/coverage | PARTIAL / NOT CONFIRMED | API count and cache coverage measured; no approved bulk snapshot |

Overall: **PARTIAL SIX GATES**. W1-006 is not accepted and Wave 1 is not
complete.

## New files

- `NOSTROY_SRO_SOURCE_PASSPORT.md`;
- `W1_006_NIGHT_RUN_REPORT.md`;
- `app/providers/nostroy_provider.py`;
- `app/models/nostroy.py`;
- `app/services/nostroy_service.py`;
- `migrations/versions/d6f7a8b9c0d1_add_nostroy_w1_006.py`;
- `scripts/inspect_nostroy.py`;
- `tests/test_nostroy_source.py`.

## Required future patches (not applied)

1. `app/models/__init__.py`: import/export `NostroyMemberCheck` so shared model
   discovery and conventional imports include the new model. Minimal patch:
   add `from app.models.nostroy import NostroyMemberCheck` and the name to
   `__all__`.
2. `app/aggregators/company_product_aggregator.py`: import
   `get_cached_nostroy_check`; add an enrichment step after existing regulatory
   enrichers. It must receive an explicit applicability result from the future
   DealContext/activity resolver, not assume every company needs SRO.
3. `main.py`: add a POST route for an explicitly applicable exact-INN refresh,
   analogous to the existing on-demand regulatory routes. Do not let a public
   unauthenticated caller force unlimited upstream requests.
4. `templates/company.html`: include a new dedicated partial and source label.
   A new partial can be added later, but the existing template integration
   point itself is tracked and was forbidden tonight.
5. Source registry integration (`app/services/source_service.py` or a new
   registry service plus its existing caller): register owner, dataset and
   source metadata only after scope/access review.

These patches are necessary for product/browser Gate 5, but applying them
would violate the “new files only” constraint.

## Blockers and final Git record

- product scope requires user review;
- commercial automation/cache/republication rights require Legal/Access review;
- undocumented API has no published limits/SLA;
- company applicability resolver and UI integration require existing-file edits;
- bulk coverage/integrity is not confirmed.

Final verification: `465 passed`; compileall PASS; `git diff --check` PASS;
tracked modifications/deletions: none; exactly the eight new files listed
above were committed. Implementation commit:
`6bb7dcca75c26199ebcc116e2631bb12d50aec36`. Open PR:
`https://github.com/kuzmvnew/kontragent/pull/36`. CI for the implementation
commit: `Python tests=success`, both `acceptance=success`. Merge status: not
merged by design. PR must remain open and must not be auto-merged.

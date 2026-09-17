# Wave 1 handoff — 2026-09-16

Purpose: authoritative working handoff for the next Kontragent chat. Updated after W1-006 local SIX GATES PASS. Status only; no change to architecture or roadmap order.

## Repository

- Repository: `kuzmvnew/kontragent`
- Branch: `main`
- W1-001 accepted code baseline: `8937cd7bafbc67b2accd2ebf185ced8418c45d2d` (PR #26)
- Documentation was updated after that commit; before development, fetch current `main` and use the newest HEAD.

## Permanent acceptance protocol

Every source is accepted separately by six gates:

1. automated tests;
2. real company / real official-source response;
3. PostgreSQL save and independent readback;
4. correct distinction between found / not_found / not_applicable / unavailable or equivalent source semantics;
5. real company card opened in an actual browser;
6. record counts, dates and Master Registry coverage.

Do not merge statuses: code ready / real data loaded / browser checked / auto-update configured are separate facts.

## Local environment

- Mac project path: `/Users/mikhailkuznetsov/Documents/kontragent`
- PostgreSQL database: `kontragent`
- Alembic observed during W1-001 acceptance: `e5a7f4c2b9d1`
- Master Registry in the local W1-001 report: `6,781,485`
- Home Windows worker setup is a separate future operational task.

## W1-001 — CBR Warning List

Status: **ACCEPTED / SIX GATES PASS on user's Mac**.

Local run on 2026-09-16:

- official full JSON HTTP 200;
- source/imported/snapshot records: `27,163 / 27,163 / 27,163`;
- rejected: `0`;
- duplicates: `0`;
- conflicting duplicates: `0`;
- usable INN: `2,995`;
- without usable INN: `24,168`;
- exact-INN linked source records: `857`;
- exact-INN linked unique Master Registry companies: `857`;
- source records with INN but unmatched: `2,138`;
- Master Registry: `6,781,485`;
- retrieval/data date: `2026-09-16` (retrieval date, not an official cut-off date for the entire source);
- entry dates: `2021-02-01` through `2026-09-16`;
- update dates: `2021-04-29` through `2026-09-16`;
- snapshot SHA-256: `f9b5d0af8c19abed44f6e65350a9da358c2ff53cf686bd0efb5c34c275bdc81f`.

Real found example:

- INN `0105064330`;
- ООО "ИНВЕСТКАПИТАЛ24";
- CBR ID `9806`;
- exact-INN `found`;
- Chromium HTTP 200, visible block, no page errors.

Real snapshot absence example:

- INN `9102309919`;
- exact-INN `not_found` against the dated successful snapshot;
- Chromium HTTP 200, visible limitation text, no page errors.

State semantics:

- `not_found` is only absence from the dated exact-INN snapshot;
- it is not proof of licence or safety;
- records without INN are not fuzzy-matched;
- source applies to legal entities and individual entrepreneurs, so business non-applicability is N/A for valid LE/IP INNs;
- failure/unknown is not converted to `is_listed=false`;
- rollback and failed-download preservation are covered by regression tests.

Auto-update: **NOT_CONFIGURED**.

Detailed evidence: `docs/W1_001_ACCEPTANCE.md`.

## W1-002 — CBR FinOrg

W1-002 CBR FinOrg: ACCEPTED / SIX GATES PASS на пользовательском Mac 16.09.2026; PostgreSQL/Chromium/coverage PASS, 435 tests. Подробности: docs/W1_002_ACCEPTANCE.md.

## W1-003 — FNS SME support recipients

W1-003 ФНС — МСП, получатели поддержки: ACCEPTED / SIX GATES PASS на пользовательском Mac 16.09.2026, code de32573 (PR #29). Snapshot 15.09.2026; PostgreSQL 11 925 998 фактов ЮЛ/ИП; Master Registry 6 781 485, exact-INN связаны 1 897 870 сущностей. Подробности: docs/W1_003_ACCEPTANCE.md.

## W1-004 — Roszdravnadzor

W1-004 Росздравнадзор: ACCEPTED / SIX GATES PASS на пользовательском Mac 16.09.2026. Scope A–D, PostgreSQL `kontragent`, Alembic `a9c4e6f8b201`, 450 tests, Chromium и coverage PASS. Подробности: docs/W1_004_ACCEPTANCE.md.

## W1-005—W1-006

W1-005 Роскомнадзор — `IN PROGRESS / SOURCE-BLOCKED (B, C)`; A/D/E/F подтверждены, B/C официального сервера остаются `unavailable`. W1-006 НОСТРОЙ / НОПРИЗ / СРО — `ACCEPTED / SIX GATES PASS` на Mac: 472 tests, PostgreSQL `kontragent`, Alembic `e7a8b9c0d1e2`, три реальные Chromium-карточки без page errors, private person records `1`, public exposure `0`. Протокол: `docs/W1_006_ACCEPTANCE.md`. Wave 1 остаётся открытой из-за W1-005 B/C.

## Boundaries

- Phase 3: completed in agreed scope.
- Phase 4: ACTIVE.
- Current priority: finish Wave 1; then public company card/site UX; then the first 10,000 SEO pages; only then Wave 2.
- RNP/EIS: deferred until official access.
- DaMIA: do not connect now.
- Person / Leads / CRM / Enterprise: do not start.
- Architecture and roadmap order: do not change without explicit user approval.


### W1-002 final Mac acceptance — 16.09.2026
W1-002 is ACCEPTED / SIX GATES PASS on the user Mac. Real found exact-INN: 9706063520 (БАНК ЭЛЕМЕНТ (ООО), 2 licences/rights); real not_found: 9102309919. PostgreSQL kontragent reread PASS; Chromium found/not_found HTTP 200, page_errors=[]; 435 tests PASS. On-demand coverage is dated cache coverage, not a bulk CBR registry. auto_update=NOT_CONFIGURED. Source protocol: docs/W1_002_ACCEPTANCE.md.

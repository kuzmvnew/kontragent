# Wave 1 handoff — final closure 2026-09-17

Purpose: authoritative final handoff for Wave 1. Wave 1 is **CLOSED / ACCEPTED BY EXPLICIT PROJECT DECISION**. The closure does not rewrite individual source evidence; W1-005 B/C remains a deferred external-source exception. See `WAVE1_CLOSURE_DECISION.md`.

## Repository

- Repository: `kuzmvnew/kontragent`
- Branch: `main`
- W1-001 accepted code baseline: `8937cd7bafbc67b2accd2ebf185ced8418c45d2d` (PR #26)
- W1-006 merged baseline before closure docs: `59d44082a6d71158ce79a2247a3d9cb4970cc581` (PR #36)
- Before any next development task, fetch current `main` and use the newest HEAD.

## Permanent acceptance protocol

Every source is accepted separately by six gates:

1. automated tests;
2. real company / real official-source response;
3. PostgreSQL save and independent readback;
4. correct distinction between found / not_found / not_applicable / unavailable or equivalent source semantics;
5. real company card opened in an actual browser;
6. record counts, dates and Master Registry coverage.

Do not merge statuses: code ready / real data loaded / browser checked / auto-update configured are separate facts.

Wave-level governance may explicitly disposition an external blocker without falsifying the source-level Six Gates result. That rule was used only to close Wave 1 with W1-005 B/C carried as a deferred exception.

## Local environment

- Mac project path: `/Users/mikhailkuznetsov/Documents/kontragent`
- PostgreSQL database: `kontragent`
- Alembic at final W1-006 acceptance: `e7a8b9c0d1e2`
- Master Registry observed at final W1-006 checkpoint: `6,781,487`
- Home Windows worker setup is a separate operational task.

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
- Master Registry at that acceptance point: `6,781,485`;
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

W1-003 ФНС — МСП, получатели поддержки: ACCEPTED / SIX GATES PASS на пользовательском Mac 16.09.2026, code de32573 (PR #29). Snapshot 15.09.2026; PostgreSQL 11 925 998 фактов ЮЛ/ИП; Master Registry на той точке 6 781 485, exact-INN связаны 1 897 870 сущностей. Подробности: docs/W1_003_ACCEPTANCE.md.

## W1-004 — Roszdravnadzor

W1-004 Росздравнадзор: ACCEPTED / SIX GATES PASS на пользовательском Mac 16.09.2026. Scope A–D, PostgreSQL `kontragent`, Alembic `a9c4e6f8b201`, 450 tests, Chromium и coverage PASS. Подробности: docs/W1_004_ACCEPTANCE.md.

## W1-005 — Roskomnadzor

Final Wave 1 disposition: **DEFERRED EXCEPTION / SOURCE-BLOCKED (B, C)**.

- A/D/E/F implemented and verified.
- Official B/C responses remain incomplete and are correctly represented as `unavailable`.
- W1-005 itself is **not** relabeled SIX GATES PASS.
- The external blocker no longer blocks Wave 1 closure by explicit project decision.
- Revisit B/C later when the official source is complete/stable; this does not automatically reopen Wave 1.

## W1-006 — NOSTROY / NOPRIZ / SRO

Status: **ACCEPTED / SIX GATES PASS on user Mac**.

- 472 tests PASS;
- PostgreSQL `kontragent`;
- Alembic `e7a8b9c0d1e2`;
- three real Chromium company cards HTTP 200, `page_errors=[]`;
- private person records `1`;
- public person exposure `0`;
- protocol: `docs/W1_006_ACCEPTANCE.md`.

## Wave 1 closure

**Wave 1 is officially CLOSED / ACCEPTED BY EXPLICIT PROJECT DECISION on 17.09.2026.**

Five steps passed their individual Six Gates. W1-005 B/C is a documented deferred external-source exception. The closure decision is authoritative in `WAVE1_CLOSURE_DECISION.md`.

## Boundaries and next stage

- Phase 3: completed in agreed scope.
- Phase 4: ACTIVE.
- Wave 1: CLOSED / ACCEPTED.
- Do not start a new Wave source now.
- Follow `POST_WAVE1_PRODUCT_PLAN.md` and `WAVE_IMPLEMENTATION_PLAN.md`.
- Immediate order: freeze/synchronize post-Wave1 specs → Auto-update / Data Readiness → Company Card v2 + Risk/Summary/Report → Security/Legal/Product gates → first 10k SEO → later Company B2B Core → Wave 2 at its approved gate.
- RNP/EIS: deferred until official access.
- DaMIA: do not connect without a separate decision.
- Private person evidence may be retained in a minimized closed container but must not be exposed publicly without a separate legal/product approval.
- Architecture and roadmap order: do not change without explicit user approval.

### W1-002 final Mac acceptance — 16.09.2026
W1-002 is ACCEPTED / SIX GATES PASS on the user Mac. Real found exact-INN: 9706063520 (БАНК ЭЛЕМЕНТ (ООО), 2 licences/rights); real not_found: 9102309919. PostgreSQL kontragent reread PASS; Chromium found/not_found HTTP 200, page_errors=[]; 435 tests PASS. On-demand coverage is dated cache coverage, not a bulk CBR registry. auto_update=NOT_CONFIGURED. Source protocol: docs/W1_002_ACCEPTANCE.md.

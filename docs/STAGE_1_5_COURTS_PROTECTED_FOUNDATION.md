# Stage 1.5 — courts and protected public sources checkpoint

Date: 2026-09-17

Migration: `c3d4e5f6a7b8`

Verification baseline: 531 automated tests passed; Alembic is at head; real Chromium rendered the C1 golden result plus the AVTOVAZ C2/E/F/H blocks. PostgreSQL contains live C1, Moscow C2, H address context, and completed E/F results.

## Invariants

- Every new source is user-triggered, on-demand and cache-first.
- Company-card GET, search, SEO and background jobs make zero new external requests.
- No mass crawl, CAPTCHA bypass, stealth, proxy rotation or fingerprint evasion exists.
- A challenge, timeout, HTTP error or parse ambiguity never becomes `not_found`.
- Person matching remains private/manual-review unless a stable official identifier exists.

## C1 Arbitration

`ArbitrationCourtProvider` keeps Court v1 vendor-neutral. `CheckoArbitrationProvider` requests one page per click (`limit=100`), initially for the preceding 12 months. Loaded sample and full-period completion are separate stored fields.

Current acceptance:

- tests: PASS;
- real source: PASS; one authorised request for exact INN `1215214540` returned 7 cases;
- PostgreSQL: PASS for live write, independent readback and cache-only refresh;
- browser: PASS; 7/7 cases, dates, complete period and official KAD links rendered;
- coverage: 2025-09-17…2026-09-17, page 1/1, complete; progressive loading remains one explicit page per click for larger results;
- security: credential stayed out of URLs, browser, logs, repository, docs and persisted data;
- limitation: Checko is a bridge rather than official source of truth; broad display/republication rights remain a later legal decision;
- future paid: Casebook/SPARK/Kontur only after separate approval.

## C2 General-jurisdiction courts

The provider family includes Moscow official, shared regional sudrf exact-identifier contract, central GAS status adapter and SudAct discovery marker. A real Moscow query for AVTOVAZ yielded three official case links; each is stored with medium confidence because matching is exact full legal name but the list does not expose INN.

Current acceptance:

- tests: PASS;
- real official source: PASS, 3 cases;
- PostgreSQL write/read: PASS;
- browser: official portal query and product card PASS;
- coverage: first 100 results of the targeted Moscow query; exact INN/OGRN form contracts confirmed for SPb and Sverdlovsk;
- limitation: bounded direct HTTP received empty replies and visible official searches required CAPTCHA; no bypass, no regional results, no national completeness;
- future paid: Kontur Focus `/api3/generalCourtCases`, Casebook, SPARK.

## E FNS account suspension

The official visible-browser experiment returned 10 active rows for INN `7702059544` even though requester BIK `044525161` differed from all five returned decision BIKs. This establishes requester-context semantics. A subsequent request produced CAPTCHA and stopped.

Current acceptance:

- tests: PASS for fail-closed states;
- real official source: PASS, positive result;
- PostgreSQL: schema/session lifecycle PASS; golden result not linked because company is absent from master registry;
- browser: official positive and AVTOVAZ dated negative PASS;
- coverage: current FNS suspension decisions only, not every account/authority;
- limitation: a future challenge still requires manual same-session resume;
- future paid/free bridge: none accepted; Checko `/company` has no published suspension field.

## F Bank of Russia ZSK

The ordinary browser completed the official request for AVTOVAZ and returned a dated absence of high-risk information. The result is cached in PostgreSQL. Product wording remains limited to presence/absence of CBR high-risk information.

Current acceptance:

- tests: PASS;
- real official result: PASS, dated negative;
- PostgreSQL: PASS for completed result/evidence;
- browser: official form/result PASS;
- coverage: point-in-time official high-risk presence only;
- limitation: future challenges remain human-only;
- future paid: Kontur Focus marker; DaMIA score is not semantic-equivalent.

## Stage status

Stage 1.5 is **CLOSED / ACCEPTED** under the owner-approved C2 partial-coverage exception. C1 passes its free-bridge Six Gates; C2 retains Moscow evidence and explicitly records regional timeout/challenge without a false nationwide claim. Auto-update/Data Readiness is next and was not started here.

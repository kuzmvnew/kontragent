# Stage 1.5 — courts and protected public sources checkpoint

Date: 2026-09-17

Migration: `c3d4e5f6a7b8`

Verification at checkpoint: 523 automated tests passed; Alembic is at head; the real AVTOVAZ card rendered all four new blocks in the in-app Chromium with zero console errors. PostgreSQL contains one C2 check with three cases, one C1 access-pending check, and one ZSK challenge session.

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
- real source: published Checko method/schema confirmed, live keyed response pending;
- PostgreSQL: PASS for dated `access_pending` state;
- browser: docs and product card verified; live keyed response pending;
- coverage: 12-month paged sample, no claim of full period until all pages loaded;
- limitation: no configured free key and rights review incomplete;
- future paid: Casebook/SPARK/Kontur only after separate approval.

## C2 General-jurisdiction courts

The provider family includes Moscow official, shared regional sudrf exact-identifier contract, central GAS status adapter and SudAct discovery marker. A real Moscow query for AVTOVAZ yielded three official case links; each is stored with medium confidence because matching is exact full legal name but the list does not expose INN.

Current acceptance:

- tests: PASS;
- real official source: PASS, 3 cases;
- PostgreSQL write/read: PASS;
- browser: official portal query and product card PASS;
- coverage: first 100 results of the targeted Moscow query; other regions not checked;
- limitation: no national completeness and BSR timeout;
- future paid: Kontur Focus `/api3/generalCourtCases`, Casebook, SPARK.

## E FNS account suspension

The official visible-browser experiment returned 10 active rows for INN `7702059544` even though requester BIK `044525161` differed from all five returned decision BIKs. This establishes requester-context semantics. A subsequent request produced CAPTCHA and stopped.

Current acceptance:

- tests: PASS for fail-closed states;
- real official source: PASS, positive result;
- PostgreSQL: schema/session lifecycle PASS; golden result not linked because company is absent from master registry;
- browser: official result PASS; embedded same-session hand-off/resume pending;
- coverage: current FNS suspension decisions only, not every account/authority;
- limitation: human challenge and product browser ownership;
- future paid/free bridge: none accepted; Checko `/company` has no published suspension field.

## F Bank of Russia ZSK

The ordinary browser reached mandatory SmartCaptcha with AVTOVAZ data filled. `challenge_required` was saved in PostgreSQL. The product wording is limited to presence/absence of CBR high-risk information.

Current acceptance:

- tests: PASS;
- real official result: pending human solve;
- PostgreSQL: PASS for challenge state/evidence;
- browser: official form/challenge PASS, same-session resume pending;
- coverage: point-in-time official high-risk presence only;
- limitation: no completed result yet;
- future paid: Kontur Focus marker; DaMIA score is not semantic-equivalent.

## Stage status

Stage 1.5 is **IN PROGRESS**. C2 has a usable free official partial foundation. C1 still needs a lawful free key/live probe. E and F still need the visible browser session to be controlled/resumed end-to-end in the product, and F needs one human-completed official result. No merge is permitted before the final acceptance review.

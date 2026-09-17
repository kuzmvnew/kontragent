# Stage 1.5 acceptance matrix

Date: 2026-09-17

Stage status: **CLOSED / ACCEPTED**

Final local regression: **534 tests passed**. Alembic: `c3d4e5f6a7b8 (head)`. Master Registry: **6,781,487 companies**.

| Workstream | Final status | Six-gate evidence | Remaining limitation |
|---|---|---|---|
| A1 Tax Debt | ACCEPTED / SIX GATES PASS | official FNS snapshot; PostgreSQL; state semantics; Chromium; dated coverage | auto-update belongs to the next stage |
| A2 Tax Offences | ACCEPTED / SIX GATES PASS | official FNS snapshot; PostgreSQL; no invented offence type; Chromium | auto-update belongs to the next stage |
| B1 FSSP | INVENTORY COMPLETE / LEGACY NOT FOUND | repository/history/local DB inventory complete; no false integration claim | future implementation backlog; not a Stage 1.5 blocker |
| B2 Fedresurs/EFRSB | INVENTORY COMPLETE / LEGACY NOT FOUND | repository/history/local DB inventory complete; no events synthesized | future implementation backlog; not a Stage 1.5 blocker |
| C1 Arbitration | ACCEPTED FOUNDATION / CHECKO FREE BRIDGE | tests; one real Checko request; PostgreSQL readback/cache; found/not_found/unavailable/quota semantics; Chromium; counts/dates/coverage | Checko is a commercial bridge, not the official source of truth; rights review remains required before broad republication |
| C2 General Courts | FOUNDATION ACCEPTED / PARTIAL TARGETED COVERAGE / REGIONAL SOURCE TIMEOUT-CHALLENGE | Moscow: 3 real official cases, PostgreSQL and Chromium; SPb/Sverdlovsk: exact INN/OGRN form contract confirmed in visible browsers | regional HTTP returned empty replies and both visible searches required CAPTCHA; no bypass; no nationwide claim |
| D Bankruptcy/Liquidation | ACCEPTED FOUNDATION | 15-state evidence model; migration; source/date/evidence; liquidation remains distinct from bankruptcy | event adapters remain future source work |
| E FNS Suspensions | HUMAN-ASSISTED PRODUCT FLOW PASS | positive official capability; dated AVTOVAZ negative; PostgreSQL/cache/card; challenge fail-closed | future CAPTCHA requires manual same-session action |
| F CBR ZSK | HUMAN-ASSISTED PRODUCT FLOW PASS | official dated AVTOVAZ negative; PostgreSQL/cache/card; challenge fail-closed | point-in-time public high-risk information only |
| G PRIME Disclosure | ACCEPTED / SIX GATES PASS | real exact-INN PRIME page; PostgreSQL; conservative document projection; Chromium | none for Stage 1.5 |
| H Company Facts | COMPLETE FOUNDATION / PARTIAL SOURCE COVERAGE | evidence-backed vocabulary/hash; legal address; normalized address/context; disclosure integration; conservative nulls | lawful source mappings remain partial; missing facts are not inferred |

## C1 live acceptance

- Golden INN: `1215214540`.
- Period: 2025-09-17 through 2026-09-17; exact INN; `limit=100`; page 1; newest first.
- External requests actually used: **1**.
- Result: `found`; provider-reported cases **7**; loaded **7**; pages **1/1**; coverage complete.
- Roles: claimant **1**, defendant **6**.
- Claim amounts in the complete 12-month result: total **2,778,964.84**, largest **956,895.60**. Claim amount is not debt.
- New cases: 30d **0**, 90d **2**, 365d **7**.
- Independent PostgreSQL readback and fresh refresh were cache hits and made no extra request.
- Company-card GET and reload were cache-only; Chromium showed all seven cases and official KAD links.
- The API key was used only in the acceptance process. It was not committed, documented, logged, sent to the browser, stored in PostgreSQL, or retained in a URL.
- Local quota guard proves exhausted quota makes **0** requests and returns `unavailable`, never `not_found`; an HTTP-200 Checko API error is also fail-closed.
- The golden result had only one page, so no real page-2 request was necessary. Contract tests prove that one deepen action requests exactly one explicit next page and never preloads it.

## C2 bounded regional acceptance

- Moscow: accepted targeted first-100 path; 3 official AVTOVAZ cases persisted and rendered.
- Saint Petersburg: one direct HTTP attempt returned an empty reply. The official visible portal loaded; its form exposed exact INN/OGRN fields; the submitted targeted query stopped at CAPTCHA.
- Sverdlovsk Region: the same bounded sequence produced an empty HTTP reply, then a visible official form with exact INN/OGRN fields and a CAPTCHA requirement.
- No CAPTCHA solving, bypass, stealth, proxy rotation, fingerprint evasion or mass crawl was used.
- This is accepted partial targeted coverage under the owner-approved regional-source exception. It is not nationwide coverage and a challenge is not a clean `not_found`.

## Closure decision

The Stage 1.5 completion criteria are satisfied: C1 passes the accepted free-bridge minimum, C2 has an explicitly accepted safe partial-coverage disposition, A1/A2/D/E/F/G/H retain their accepted results, B1/B2 are classified backlog, and source failures remain distinct from negative results.

The next stage is **AUTO-UPDATE / DATA READINESS**. It was not started in this change.

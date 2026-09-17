# STAGE 1.5 HANDOFF — Kontragent

Date: 2026-09-17
Status: CLOSED / ACCEPTED

## Baseline

Wave 1 is officially closed by `WAVE1_CLOSURE_DECISION.md`.

GitHub main at the start of this documentation sync was:

`79d9e3e9aae588e7df04af46c34d796853bf292f`

Commit message:

`Docs: officially close and accept Wave 1`

Before coding Stage 1.5, always re-fetch current `main`; do not assume this SHA remains current after documentation merge.

## Wave 1 final status

| Step | Source | Status |
|---|---|---|
| W1-001 | CBR Warning List | ACCEPTED / SIX GATES PASS |
| W1-002 | CBR FinOrg | ACCEPTED / SIX GATES PASS |
| W1-003 | FNS SME support recipients | ACCEPTED / SIX GATES PASS |
| W1-004 | Roszdravnadzor | ACCEPTED / SIX GATES PASS |
| W1-005 | Roskomnadzor | DEFERRED EXCEPTION / SOURCE-BLOCKED B/C; A/D/E/F verified |
| W1-006 | NOSTROY / NOPRIZ / SRO | ACCEPTED / SIX GATES PASS |

Wave 1 is CLOSED even though W1-005 B/C remains an external-source exception. Do not relabel B/C as success or `not_found`.

## Pre-Wave1 / existing source inventory

These sources already exist at least at code/data/product-foundation level and must not be blindly reimplemented:

| Source/block | Current documented state | Stage 1.5 action |
|---|---|---|
| Master Registry | ready | reuse |
| FNS Tax Debt | ACCEPTED / SIX GATES PASS on 2026-09-17 | reuse; protocol `docs/STAGE_1_5_A1_TAX_DEBT_ACCEPTANCE.md` |
| FNS Tax Offences | ACCEPTED / SIX GATES PASS on 2026-09-17 | reuse; protocol `docs/STAGE_1_5_A2_TAX_OFFENCE_ACCEPTANCE.md` |
| FNS Paid Taxes | connected | reuse |
| FNS Revenue & Expenses | connected | reuse |
| FNS Average Employees | connected | reuse |
| SME Registry | connected | reuse |
| SNR / SNRIP | completed | reuse |
| Disqualified Persons | completed | reuse |
| NPD | completed on-demand/cache | reuse |
| ERKNM | completed | reuse |
| RNP/EIS | deferred external access blocker | do not substitute with paid mirror by default |
| FSSP | inventory complete: `NOT_FOUND`; only disabled catalog metadata exists | do not claim integration; future work requires approved official/public access path and six-gate acceptance |
| Fedresurs/EFРSB | inventory complete: `NOT_FOUND`; only disabled catalog metadata and planning specs exist | do not claim integration; future work requires access/cost decision and six-gate acceptance |

## Stage 1.5 work order

Recommended implementation order:

1. Tax Debt final acceptance.
2. Tax Offences final acceptance.
3. FSSP inventory/recovery decision.
4. Fedresurs/EFРSB inventory/recovery decision.
5. Bankruptcy/liquidation event semantics on top of recovered/new evidence.
6. Arbitration Courts v1.
7. Courts of General Jurisdiction v1.
8. FNS bank-account suspension decisions.
9. Bank of Russia public high-risk/KYC technical probe and production-mode decision.
10. Free corporate-disclosure foundation.
11. Normalise company contacts/addresses/public bank details/mass address-director-founder facts needed by later Risk/Summary.
12. Full Stage 1.5 acceptance review.

FSSP inventory was completed on 2026-09-17. See `FSSP_STAGE_1_5_INVENTORY.md`. No working or reusable implementation was found in the current tree, visible history, unreachable commits, local backup, or PostgreSQL; the existing disabled source/dataset rows are catalog placeholders only.

Fedresurs/EFRSB inventory was completed on 2026-09-17. See `FEDRESURS_STAGE_1_5_INVENTORY.md`. No working or reusable implementation was found in the same repository/history/local/database surfaces; the disabled catalog rows and financial-distress specification are not an integration.

Bankruptcy/liquidation semantics were accepted on 2026-09-17. See `docs/STAGE_1_5_D_LEGAL_EVENTS_ACCEPTANCE.md`. Migration `f8b9c0d1e2f3` adds a 15-state evidence-backed event model and the current product can project stored events conservatively. No source record was fabricated: the table remains empty until a future adapter passes its own access review and six gates.

Arbitration Courts v1 passed the agreed free-bridge Six Gates: one authorised live request for INN `1215214540` returned 7/7 cases for the 12-month period, persisted and reread from cache, then rendered in Chromium. The credential was not retained or exposed. Checko remains a bridge, not the official source of truth or a paid dependency.

C2 has a real Moscow adapter/result plus targeted routing contracts for Saint Petersburg and Sverdlovsk official sudrf portals. Bounded regional HTTP calls returned empty replies and visible exact-INN/OGRN submissions required CAPTCHA. No bypass occurred; partial targeted coverage is accepted and nationwide coverage is not claimed.

The official FNS account-suspension service produced one real dated negative browser result, then required CAPTCHA; unattended production access is blocked and no bypass was attempted. See `FNS_ACCOUNT_SUSPENSION_STAGE_1_5_ACCESS.md`.

The Bank of Russia ZSK and FNS BANKINFORM visible-browser flows both completed for AVTOVAZ and their dated negative results are stored in PostgreSQL/cache. Future CAPTCHA challenges remain human-only.

Checkpoint `c3d4e5f6a7b8` adds vendor-neutral C1/C2 providers, dated court caches, user-triggered company-card controls, and common `InteractiveProtectedSourceSession` storage. The official Moscow adapter produced and cached three AVTOVAZ cases. The Checko arbitration bridge enforces one 100-row page per click and passed live acceptance. The official FNS browser probe produced 10 current suspension rows for golden INN `7702059544`; requester BIK was proven not to filter decision BIKs. See `docs/STAGE_1_5_COURTS_PROTECTED_FOUNDATION.md`.

The PRIME free/public corporate-disclosure foundation passed Six Gates for targeted exact-INN retrieval. Migration `a1c2d3e4f5b6` adds dated disclosure checks; the company card and API expose source-attributed profile/document metadata without mirroring documents. See `PRIME_CORPORATE_DISCLOSURE_SOURCE_PASSPORT.md` and `docs/STAGE_1_5_G_CORPORATE_DISCLOSURE_ACCEPTANCE.md`.

Migration `b2c3d4e5f6a7` adds evidence-backed normalized company public facts. Real PRIME legal/postal addresses were persisted for AVTOVAZ. The model vocabulary covers later contacts, mass indicators, disclosed bank details, and relationships, but those source mappings remain partial and must not be inferred. See `docs/STAGE_1_5_H_COMPANY_FACT_NORMALIZATION.md`.

Parallel work is allowed only when it does not risk database/schema conflicts or duplicate an existing source implementation.

## Permanent six-gate protocol

Every new/recovered source:

1. tests;
2. real public/official source result;
3. PostgreSQL write/readback;
4. correct `found / not_found / not_applicable / unavailable` semantics;
5. real Chromium/product verification;
6. record/date/coverage audit.

Code-ready, data-loaded, product-visible and auto-update-ready are separate statuses.

## Hard guardrails

- No Wave 2 during Stage 1.5.
- Free/public/official first.
- No paid source becomes a dependency without explicit product-owner decision.
- Do not duplicate FSSP/Fedresurs before inventory.
- Court claims are not confirmed debt.
- Liquidation is not bankruptcy.
- Source failure is never `not_found`.
- Risk is separate from `Полнота проверки`.
- Quantitative conclusions later must show actual numbers/period/calculation.
- Person/private data remains outside public company/SEO projection unless separately approved.

## Documents to read before Stage 1.5 coding

1. `WAVE1_CLOSURE_DECISION.md`
2. `WAVE1_STATUS.md`
3. `INTERMEDIATE_STAGE_1_5.md`
4. `WAVE_IMPLEMENTATION_PLAN.md`
5. `POST_WAVE1_PRODUCT_PLAN.md`
6. `SOURCE_ACCESS_COST_GATE.md`
7. `CORPORATE_DISCLOSURE_SOURCE_PLAN.md`
8. `COMPANY_CARD_V2_SCOPE.md`
9. `RISK_ENGINE_SPEC.md`
10. `SUMMARY_ENGINE_SPEC.md`
11. `COURT_INTELLIGENCE_SPEC.md`
12. `FINANCIAL_DISTRESS_SPEC.md`
13. `PROJECT_STATUS.md`

## Definition of Stage 1.5 completion

Stage 1.5 closed on 2026-09-17 after the criteria in `INTERMEDIATE_STAGE_1_5.md` were met with the explicit C2 partial-coverage disposition. See `STAGE_1_5_ACCEPTANCE.md`.

After that the approved order is:

`Auto-update/Data Readiness -> Risk Engine -> Summary Engine -> Company Card v2 -> Report v1 -> Security Gate -> Legal Gate -> Product/Card Acceptance -> 10k SEO -> Lists/Bulk -> Monitoring -> Workspace -> Wave 2`.

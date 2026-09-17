# STAGE 1.5 ACCEPTANCE — Kontragent

Date: 2026-09-17

Decision: **CLOSED / ACCEPTED**

Stage 1.5 is accepted under the completion criteria and the explicit product-owner disposition for regional courts. The authoritative detailed evidence is in `docs/STAGE_1_5_ACCEPTANCE_MATRIX.md`.

Final local verification: **534 tests passed**; Alembic `c3d4e5f6a7b8 (head)`; PostgreSQL database `kontragent`; Master Registry **6,781,487** companies.

## Final workstream status

| Workstream | Status |
|---|---|
| A1 Tax Debt | ACCEPTED / SIX GATES PASS |
| A2 Tax Offences | ACCEPTED / SIX GATES PASS |
| B1 FSSP | INVENTORY COMPLETE / LEGACY NOT FOUND |
| B2 Fedresurs/EFRSB | INVENTORY COMPLETE / LEGACY NOT FOUND |
| C1 Arbitration | ACCEPTED FOUNDATION / CHECKO FREE BRIDGE |
| C2 General Courts | FOUNDATION ACCEPTED / PARTIAL TARGETED COVERAGE / REGIONAL SOURCE TIMEOUT-CHALLENGE |
| D Bankruptcy/Liquidation | ACCEPTED FOUNDATION |
| E FNS Account Suspension | HUMAN-ASSISTED PRODUCT FLOW PASS |
| F CBR ZSK | HUMAN-ASSISTED PRODUCT FLOW PASS |
| G PRIME | ACCEPTED / SIX GATES PASS |
| H Company Facts | COMPLETE FOUNDATION / PARTIAL SOURCE COVERAGE |

## Acceptance boundaries

- Checko is a free bridge provider, not the official court source of truth. KAD links remain the official case references where available.
- Court claim amounts are not confirmed debt.
- C2 is targeted partial coverage, not a nationwide clean result. Regional CAPTCHA/timeout states are unavailable, never `not_found`.
- E/F remain human-assisted, challenge-safe product flows; no protection bypass is introduced.
- B1/B2 and partial H source coverage are explicit backlog, not concealed completion claims.
- Auto-update is a separate next stage and was not started.

Closure is merged only after the final regression, Alembic check, clean diff, green PR #39 CI and mergeability checks pass.

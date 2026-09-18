# Product Recovery — factual source status

Date: 2026-09-18

Status: **STAGE 1.6 AUDIT CLOSED / PRODUCT RECOVERY NOT ACCEPTED**

Code checkpoint: `d8b1d38`

## Runtime matrix

| Capability | Applicability | Golden-40 result | Runtime status |
|---|---|---:|---|
| Registration | Mandatory always | 40 terminal | Active |
| Bankruptcy / EFRSB | Mandatory always | 19 found; 21 unavailable | Official REST implemented; credentials absent; bridge partial |
| FSSP | Mandatory always | 16 found; 24 unavailable | One preserved official positive; remaining negative closure blocked |
| Tax debt | Mandatory always | 40 terminal | Active |
| Tax offences | Mandatory always | 40 terminal | Active |
| Finance | Mandatory always | 40 terminal | Active |
| Arbitration | Mandatory always | 1 partial; 39 unavailable | Recent bridge snapshot partial; authorized access pending |
| General courts | Mandatory always | 19 partial; 21 unavailable | Regional official coverage only |
| CBR ZSK | Mandatory always | 40 unavailable | Human action required |
| CBR warning list | Mandatory always | 40 not-found | Active |
| Bankinform | Optional context | 40 N/A: no bank/account/BIK context | Contextual |
| Management | Mandatory always | 40 terminal | Active |
| Licences/SRO | Mandatory if applicable | 18 N/A; 15 found; 6 partial; 1 unavailable | Partial |
| RNP/EIS | Deferred external access | 40 deferred-unavailable | Deferred, not N/A |
| ERKNM inspections | Mandatory always | 1 found; 39 not-found | Active |

## Source and evidence semantics

`OFFICIAL_DIRECT > OFFICIAL_DOWNLOADED_DATASET > AUTHORIZED_BRIDGE > DISCOVERY_ONLY`.

- Checko and Firmoteka are `AUTHORIZED_BRIDGE`, not official publishers.
- `POLICY_RULE` decides applicability only and cannot create source facts or risk.
- One selected result per capability prevents direct/bridge double counting.
- Incomplete protected sessions cannot become `found` or `not_found`.
- A regional court result does not prove nationwide absence.
- Positive FSSP bridge evidence may be retained; negative bridge evidence cannot close the capability.
- RNP is deferred rather than mislabeled N/A; Bankinform is N/A only when required transaction context is absent.

## Exact external blockers

- FSSP: current official exact-INN public flow returns CAPTCHA. One preserved 2026-09-17 positive is historical evidence, not a current live rerun.
- EFRSB: the documented production REST client is implemented; `EFRSB_API_LOGIN` and `EFRSB_API_PASSWORD` are absent.
- CBR ZSK: official public flow requires SmartCaptcha; no completed Golden-40 sessions exist.
- Arbitration: `CHECKO_API_KEY` is absent and no authorized KAD machine contract is confirmed.
- General courts: routing is regional; 21 bounded official routes did not complete and none can support a nationwide negative conclusion.
- Licences/SRO: one applicable education-licence route remains unconnected.
- EIS RNP: official recipient access is not connected and remains deferred.

No challenge was bypassed, no credential was guessed and no unavailable result was converted to absence.

## Current metrics

- Golden-40: 40/40; workflow 100%; coverage 53–88, average 65.3; positive gate 0/40 and 0/10 low-risk.
- Full regression: 671 passed; Alembic `b8c9d0e1f2a3 (head)`.
- Public runtime: known 200, unknown 404, anonymous protected 401, authorized internal 200, zero DB writes, zero external calls, no test token in logs.
- Read path: 62–69 SQL queries, p50 1035.026 ms, p95 1909.876 ms, zero external calls.
- Browser: Chromium desktop/mobile, 40 cards, exact search and group filters verified, zero console errors and no horizontal overflow.
- Evidence: `artifacts/product_recovery_final/`.

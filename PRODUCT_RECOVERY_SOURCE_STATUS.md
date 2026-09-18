# Product Recovery — factual source status

Date: 2026-09-18

Status: **STAGE 1.6 AUDIT CLOSED / PRODUCT RECOVERY NOT ACCEPTED**

## Runtime matrix

| Capability | Applicability | Golden-40 result | Runtime status |
|---|---|---:|---|
| Registration | Mandatory always | 40 terminal | Active |
| Bankruptcy / EFRSB | Mandatory always | 19 found; 21 unavailable | Partial / challenge blocked |
| FSSP | Mandatory always | 16 found; 24 unavailable | Partial / challenge blocked |
| Tax debt | Mandatory always | 40 terminal | Active |
| Tax offences | Mandatory always | 40 terminal | Active |
| Finance | Mandatory always | 40 terminal | Active |
| Arbitration | Mandatory always | 40 unavailable | Access pending |
| General courts | Mandatory always | 40 unavailable | Partial targeted coverage |
| CBR ZSK | Mandatory always | 40 unavailable | Human action required |
| CBR warning list | Mandatory always | 40 not-found | Active |
| Bankinform | Optional context | 40 N/A: no bank/account BIK context | Contextual |
| Management | Mandatory always | 40 terminal | Active |
| Licences/SRO | Mandatory if applicable | 18 N/A; 15 found/partial; 7 unavailable | Partial |
| RNP/EIS | Deferred external access | 40 deferred-unavailable | Deferred, not N/A |
| ERKNM inspections | Mandatory always | 1 found; 39 not-found | Active |

## Source precedence

`OFFICIAL_DIRECT > OFFICIAL_DOWNLOADED_DATASET > AUTHORIZED_BRIDGE > DISCOVERY_ONLY`.

`POLICY_RULE` is not an evidence source and has zero risk strength. It may remove a truly irrelevant capability from the coverage denominator, but it cannot create `found`, `not_found` or risk points. One resolved result per capability prevents direct/bridge double counting.

## Applicability basis

- Entity/profile: legal entity, IP, new company, financial organization and non-profit remain explicit catalog profiles.
- Identifier: exact INN/OGRN is required where the source supports it; a missing or mismatched identifier never becomes a negative result.
- Industry/OKVED: financial-market and Roszdrav read paths are skipped only when a known primary OKVED is outside their scope; unknown OKVED stays unresolved rather than N/A.
- Licences/SRO: construction, design/survey and regulated activity prefixes route only the relevant registries. The primary OKVED is a conservative trigger, not proof that every possible permit is satisfied.
- Deal context: Bankinform requires bank/account context plus querying-bank BIK. Procurement context does not prove RNP inapplicability.
- Source semantics: ERKNM event presence is not inherently negative; a violation signal requires evidence in the published result text.

## External blockers

- FSSP: exact-INN official request returned a CAPTCHA form.
- EFRSB: official endpoint returned HTTP 401 with QRATOR/CAPTCHA.
- CBR ZSK: official public flow requires SmartCaptcha.
- KAD: public UI is reachable but includes CAPTCHA; no documented authorized machine contract was established.
- General courts: official search endpoints timed out in the bounded probe; existing adapters remain regional and fail closed.
- Checko: `CHECKO_API_KEY` is absent.
- EIS RNP: official machine access is not connected.

These are not passes. No challenge was bypassed and no unavailable result was converted to absence.

## Current metrics

- Golden-40: 40/40, workflow 100%, coverage 51–78 (average 63.5), positive gate 0/40.
- Full regression: 655 passed.
- Read path: 62–69 SQL queries, p50 1012.274 ms, p95 1578.178 ms, 0 external calls.
- Evidence: `artifacts/stage_1_6/source_runtime_probes.json`, `artifacts/stage_1_6/golden_40/product_recovery_v3_40_report.json`.

# Stage 1.6 — Product Recovery Audit Closure & Runtime Hardening

Status: **AUDIT CLOSED / PRODUCT RECOVERY NOT ACCEPTED**

Last verified: **18.09.2026 (Europe/Moscow)**

Code checkpoint: `d92ef5a82bb42506d7b31f802f92ec64b8d8fa24` (`feat: complete stage 1.6 recovery runtime closure`)

## Decision

The mandatory Stage 1.6 audit and hardening work is complete: applicability is explicit, existing source results are wired into v3, public reads were reduced and remained side-effect free, the same Golden-40 cohort was rerun and persisted, UX was checked in a browser, and the full local regression passed.

Product Recovery itself is **not accepted**. The positive gate remains **0/40** because direct FSSP/EFRSB/ZSK and court evidence is still unavailable or incomplete. No rule, source precedence, risk weight or threshold was weakened to manufacture a pass. Company Card v2 and later stages remain blocked.

## Acceptance evidence

- Full regression: **655 passed**, exit 0; two third-party deprecation warnings and one interpreter-shutdown psycopg `ResourceWarning` were observed.
- Alembic: `b8c9d0e1f2a3 (head)`.
- Local CI parity: diff, Ruff critical rules, compileall, 413-file secret scan, dependency audit and startup smoke passed. Remote CI was not triggered because the workflow runs only on `main` or pull requests, and both were outside the authorized scope.
- Golden cohort: **40/40**, unchanged membership except the previously accepted GGP selector correction.
- Workflow completion: **100–100%**, 13/13 mandatory workflows attempted for every company.
- Evidence coverage: **51–78/100**, average **63.5/100**; previous run was 47–72, average 57.8.
- Positive gate: **0/40 overall**, **0/10 low-risk candidates**. Their coverage is 51–54, average 52.9, so a positive conclusion would be false.
- Persistence: **40** new immutable Risk/Summary v3 pairs, **0** stale-version reuses.
- Public read profile: **62–69 SQL queries**, p50 **1012.274 ms**, p95 **1578.178 ms**, **0 external calls**. Previous profile: 75–78, p50 1098.022 ms, p95 1676.589 ms.
- Public runtime tests: known GET has **0 DB writes**, unknown GET has **0 provider calls**, anonymous mutation/internal routes return **401**.
- Browser smoke: **40 cards**; search **1/40**; low-risk filter **10/40**; at most **4** grouped limitations; no internal enum/dataset/rule jargon on the first view.

## Capability policy and factual result

Applicability classes are canonical: `MANDATORY_ALWAYS`, `MANDATORY_IF_APPLICABLE`, `OPTIONAL_CONTEXT`, `DEFERRED_EXTERNAL_ACCESS`. `NOT_APPLICABLE` is removed from the denominator; deferred external access is reported separately and does not masquerade as N/A or reduce current coverage when no result exists.

| Capability | Before | After | Status | Evidence | Remaining blocker |
|---|---|---|---|---|---|
| Registration | Resolved | 40 terminal | Active | FNS dataset / bridge precedence | None in cohort |
| Bankruptcy / EFRSB | 21 unresolved | 19 found, 21 unavailable | Partial | Concrete bridge events retained; direct EFRSB challenged | Authorized non-CAPTCHA transport |
| FSSP | 24 unresolved | 16 found, 24 unavailable | Partial | Exact-INN official probe reached CAPTCHA; negative bridge still cannot close | Authorized transport and 74/96 mismatch resolution |
| Tax debt | Resolved | 40 terminal | Active | FNS downloaded dataset | None in cohort |
| Tax offences | Resolved | 40 terminal | Active | FNS downloaded dataset | None in cohort |
| Finance | Resolved | 40 terminal | Active | FNS downloaded dataset / bridge fallback | None in cohort |
| Arbitration | 40 unresolved | 40 unavailable | Blocked | KAD UI reachable; Checko credential absent | Authorized machine path or credential |
| General courts | 40 unresolved | 40 unavailable | Partial targeted | Regional routing exists; official endpoints timed out/challenged | Exact regional coverage; no nationwide negative inference |
| CBR ZSK | 40 unresolved | 40 unavailable | Human action required | Official flow requires SmartCaptcha | Authorized completed per-company flow |
| CBR warning list | Resolved | 40 not-found | Active | Official downloaded dataset | None in cohort |
| Bankinform | 40 unavailable | 40 N/A | Optional context | Current FNS form requires taxpayer INN plus querying-bank BIK | Supply real bank/account context and BIK when relevant |
| Management | Resolved | 40 terminal | Active | FNS disqualified-person dataset | None in cohort |
| Licences/SRO | 25 unresolved | 18 N/A, 15 found/partial, 7 unavailable | Mandatory if applicable | OKVED policy plus existing Roszdrav/НОСТРОЙ/НОПРИЗ and bridge facts | Close the 7 applicable gaps and confirm all required permissions |
| RNP/EIS | 40 unresolved | 40 deferred-unavailable | Deferred external access | Missing procurement context is not converted to universal N/A | Official EIS access |
| ERKNM inspections | 40 unresolved | 39 not-found, 1 found | Active | Existing official dataset now enters v3 normalization | None in cohort; absence limited to loaded periods |

## Semantics preserved

- Precedence remains `OFFICIAL_DIRECT > OFFICIAL_DOWNLOADED_DATASET > AUTHORIZED_BRIDGE > DISCOVERY_ONLY`; policy rules only decide applicability and cannot create adverse or negative source facts.
- One resolved fact per capability prevents double counting. Coverage does not multiply or suppress risk.
- The positive conclusion requires coverage ≥90, all mandatory hard checks resolved, risk <20 and **no confirmed adverse risk point**.
- Bankinform is contextual, not universally unavailable. A positive cached decision is retained even if current bank context is missing.
- Licences/SRO use conservative OKVED routing; an unknown OKVED is not treated as N/A.
- RNP is deferred rather than falsely marked N/A.
- ERKNM is now evidence-bearing; a control event itself is not adverse unless the result text confirms a violation signal.

## Exact external blockers

| Source | Observed blocker | Honest state |
|---|---|---|
| FSSP | Exact-INN HTTP 200 response was a CAPTCHA form | `AUTHORIZED_FLOW_NOT_AUTOMATED` |
| EFRSB/Fedresurs | HTTP 401 QRATOR/CAPTCHA | `AUTHORIZED_FLOW_NOT_AUTOMATED` |
| CBR ZSK | SmartCaptcha in official public flow | `HUMAN_ACTION_REQUIRED` |
| KAD arbitration | Public UI includes CAPTCHA; authorized machine contract not established | `AUTHORIZED_MACHINE_PATH_UNCONFIRMED` |
| General courts | Bounded official endpoint verification timed out; coverage remains regional | `PARTIAL_TARGETED_COVERAGE` |
| Checko | `CHECKO_API_KEY` absent | `ACCESS_PENDING` |
| EIS RNP | Official machine access not connected | `DEFERRED_EXTERNAL_ACCESS` |

No CAPTCHA was solved or bypassed; no credential was guessed; no unavailable response became `not_found`.

## Evidence artifacts

- `artifacts/stage_1_6/final_acceptance_summary.json`
- `artifacts/stage_1_6/source_runtime_probes.json`
- `artifacts/stage_1_6/golden_40/capability_applicability_policy.json`
- `artifacts/stage_1_6/golden_40/product_recovery_v3_40.{json,csv,html}`
- `artifacts/stage_1_6/golden_40/product_recovery_v3_40_report.json`
- `artifacts/stage_1_6/company_read_profile.json`
- `artifacts/stage_1_6/company_read_profile_before_optimization.json`
- `artifacts/stage_1_6/search_explain.json`

## Ordered next action

Do not start Company Card v2, Report v1, Platform 2.0, 10k enrichment, Security v2 or SEO. Resume Product Recovery only when one of the recorded external blockers changes, rerun the **same** Golden-40 cohort, and accept the product only if the real evidence satisfies the existing gate.

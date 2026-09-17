# Product Recovery architecture

Status: **IN PROGRESS**

## Product Recovery v3 increment

The compatible v3 path is now:

`SourceCapabilityCatalog -> SourceRunnerRegistry -> SourceRateGovernor -> SourceResolver -> NormalizedCheckResult -> Coverage Engine v2 -> Risk Engine v3 -> Summary Engine v3`.

The resolver applies official-direct, official-dataset, authorized-bridge and
discovery-only precedence and returns one fact per capability. Coverage and risk
are separate 0–100 values. See `PRODUCT_RECOVERY_V3_ACCEPTANCE.md` for current
runtime evidence and blockers.

The catalog contains 15 real capabilities. Coverage 0–100 is a weighted
completeness score, not “sources out of 100”; the exact capability/source count
is reported separately. Risk 0–100 is an explainable product index, not a
probability of default, bankruptcy or fraud and not a credit rating. Each risk
contribution is traceable to source, fact, rule, calculation and date.

## Code and runtime boundary

`CODE EXISTS` does not imply `RUNTIME VERIFIED`. The Source Capability Catalog,
Firmoteka adapter, Source Rate Governor, FSSP/EFRSB/CBR ZSK/Bankinform runner
foundations, Arbitration Resolver, Coverage Engine v2, Risk Engine v3 and
Summary Engine v3 exist in code. A runner counts as completed only when its real
source result is recorded for the company; runner presence alone does not close
the 40-company gate.

## Implemented foundation

`CompanyCheckOrchestrator` now defines three bounded modes:

- `QUICK`: cache/bulk facts only; no provider calls.
- `FULL`: explicit registered runners plus protected-source human-action sessions.
- `REFRESH_DUE`: invokes only registered checks whose cached state is missing, stale, blocked or errored.

Every check returns one of: `SUCCESS_FOUND`, `SUCCESS_NOT_FOUND`,
`NOT_APPLICABLE`, `STALE`, `UNAVAILABLE`, `HUMAN_ACTION_REQUIRED`,
`ACCESS_PENDING`, `SOURCE_BLOCKED`, `ERROR`.

The compatible legacy pipeline remains:

`Company -> cached facts -> freshness/due checks -> protected actions -> legal events -> courts -> coverage -> data quality -> Risk v2 -> Summary v2`.

Provider exceptions fail closed as `ERROR`. They never become `SUCCESS_NOT_FOUND`.
Risk calculation remains pure and does not perform network access.

## Current runtime boundary

The current 40-company run processed 40/40 unique companies and formed all four
buckets, but Product Recovery remains **IN PROGRESS**. Coverage is 47–80/100
(average 58/100) and the positive gate is 0/40. Unresolved mandatory runtime
checks are Arbitration 39/40, General Courts 40/40, CBR ZSK 40/40, Bankinform
40/40, FSSP 24/40 and direct bankruptcy/EFRSB 21/40.

Summary Engine v3 keeps risk and coverage separate and presents Russian labels,
`₽`, `ДД.ММ.ГГГГ` dates, specific positive checks, grouped limitations,
actionable recommendations and source/methodology drill-down.

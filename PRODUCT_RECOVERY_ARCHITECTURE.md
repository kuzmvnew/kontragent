# Product Recovery architecture

Status: **IN PROGRESS**

## Product Recovery v3 increment

The compatible v3 path is now:

`SourceCapabilityCatalog -> SourceRunnerRegistry -> SourceRateGovernor -> SourceResolver -> NormalizedCheckResult -> Coverage Engine v2 -> Risk Engine v3 -> Summary Engine v3`.

The resolver applies official-direct, official-dataset, authorized-bridge and
discovery-only precedence and returns one fact per capability. Coverage and risk
are separate 0–100 values. See `PRODUCT_RECOVERY_V3_ACCEPTANCE.md` for current
runtime evidence and blockers.

## Implemented foundation

`CompanyCheckOrchestrator` now defines three bounded modes:

- `QUICK`: cache/bulk facts only; no provider calls.
- `FULL`: explicit registered runners plus protected-source human-action sessions.
- `REFRESH_DUE`: invokes only registered checks whose cached state is missing, stale, blocked or errored.

Every check returns one of: `SUCCESS_FOUND`, `SUCCESS_NOT_FOUND`,
`NOT_APPLICABLE`, `STALE`, `UNAVAILABLE`, `HUMAN_ACTION_REQUIRED`,
`ACCESS_PENDING`, `SOURCE_BLOCKED`, `ERROR`.

The pipeline is:

`Company -> cached facts -> freshness/due checks -> protected actions -> legal events -> courts -> coverage -> data quality -> Risk v2 -> Summary v2`.

Provider exceptions fail closed as `ERROR`. They never become `SUCCESS_NOT_FOUND`.
Risk calculation remains pure and does not perform network access.

## Not yet accepted

The orchestrator contract and tests do not prove production source completion.
Full acceptance requires real EGRUL/EGRIP, Fedresurs and manually completed FSSP
flows for the 40-company matrix.

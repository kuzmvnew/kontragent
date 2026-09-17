# Firmoteka authorized bridge — source passport

Date: 2026-09-17
Status: **FIRST-CLASS AUTHORIZED BRIDGE / PRODUCT RECOVERY V3**

## Evidence and authorization

- `USER_REPORTED`: product owner reports authorization for low-load automated parsing, including coordinated multiple stable egress IPs.
- Codex did not inspect a contract or authorization letter.
- `EXTERNAL_HISTORICAL_EVIDENCE`: the dated 500-company pilot records 500/500 processed, 500/500 exact-INN pages, 100% HTTP success, no 403/429/challenge/parse errors, status 500/500, registration date 500/500, OKVED 500/500, address/region 420/500, FSSP bridge coverage 420/500 and 24 bankruptcy candidates.
- Source class: `AUTHORIZED_BRIDGE`. It is never represented as `OFFICIAL_DIRECT`.

## Accepted capabilities

`FirmotekaSourceAdapter` normalizes exact-INN cached facts for registration,
FSSP, bankruptcy events, management/ownership, financial/tax cross-checks,
licences/SRO and procurement when the corresponding structured page facts exist.

Registration closes the capability only when exact INN, status and registration
date are present. Financial and tax facts are secondary when official FNS data
exists. A Firmoteka score is not consumed by Risk Engine v3.

Concrete bankruptcy phrases (bankrupt declaration, observation, external
management, competitive proceedings or an initiated case) are
`AUTHORIZED_BRIDGE` evidence. A generic badge or “Действующее юридическое лицо”
is discovery-only and does not close the bankruptcy capability.

## Precedence

`OFFICIAL_DIRECT > OFFICIAL_DOWNLOADED_DATASET > AUTHORIZED_BRIDGE > DISCOVERY_ONLY`.

The resolver emits one result per capability. A direct FSSP result replaces the
bridge fact for risk calculation while preserving both source codes in
`resolved_by`. An unavailable direct runner allows a policy-approved bridge
fallback; it does not erase it.

## Rate and scaling policy

Observed safe baseline is one request per six seconds per IP with concurrency one.
`SourceRateGovernor` defaults Firmoteka to randomized 6–12 second intervals,
per-source/per-IP/global budgets, concurrency one, retry/backoff, Retry-After,
circuit breaking, cache and atomic checkpoint/resume.

Controlled scaling is prepared for 2 → 4 → 6 → 8 stable-IP workers with stable
shards. Future multi-IP scaling has not passed production acceptance. IP
rotation to hide blocking is prohibited. A 100k run is not authorized by this
iteration and was not started.

## Known discrepancies

The direct FSSP historical comparison for INN `0274101890` returned 96 records
versus 74 in the bridge snapshot. It remains one `MISMATCH`; it is not sufficient
to claim equivalence or general unreliability. At least 20–40 direct comparisons
and semantic analysis of active/all/completed/role/date differences remain open.

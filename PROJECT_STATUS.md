# PROJECT STATUS — GENERATED — DO NOT EDIT MANUALLY

> Canonical source: [`project_status.yaml`](project_status.yaml).
> Regenerate with `uv run python scripts/project_status.py update`.

- Schema version: `1.0.0`
- Generated at: `2026-10-08T09:24:17Z`
- Canonical main SHA: `d51622288968a63cf2f91196564550df620f0b47`

Layer states are independent. A merge is not deployment, QA acceptance is
not deployment, and production deployment is not user-visible verification.

## CODE

| Field | Value |
|---|---|
| State | VERIFIED |
| Main SHA | d51622288968a63cf2f91196564550df620f0b47 |
| Latest relevant merged PR | #137 — https://github.com/kuzmvnew/kontragent/pull/137 |
| Merge SHA | d51622288968a63cf2f91196564550df620f0b47 |
| Merged at | 2026-10-08T08:49:19Z |
| Operational migration head in code | b5d7f9a1c3e6 |
| Public migration head in code | public_0002 |
| Release-capable artifact state | PRESENT |
| Release-capable artifact | nextcompany-canonical-operational-cohort-40 |
| Artifact path | docs/releases/public-v1-cohort-40.json |
| Artifact SHA-256 | 8f44d69ad716b3481f92287e32df1c51f41aeecca5bd449265520d7656f40752 |

### CODE capabilities

| Capability | Status | Evidence |
|---|---|---|
| `transition-aware-public-releases` | `MERGED` | PR #109 / merge 9ca2ab22b6597ea0e4c846de33e34946c325d59e |
| `public-card-binding` | `MERGED` | PR #108 / merge bd9ee511d1180f36d388b042216def8207aeda94 |
| `post-migration-factory-recovery` | `MERGED` | PR #107 / merge 0ac7607dbb6b33c1cbdf1cc1604a28aee8a86882 |
| `workspace-bulk-check` | `MERGED` | PR #137 / merge d51622288968a63cf2f91196564550df620f0b47 |
| `workspace-users-roles-settings` | `NOT_MERGED` | 03D service, PostgreSQL migration, tenant-safe HTML/API, concurrent owner and invitation tests, real Chromium flows, and clean 2240-pass repository suite on the dedicated branch |

## QA

| Field | Value |
|---|---|
| State | NOT_ACCEPTED |
| Accepted head SHA | UNKNOWN |
| QA task ID | UNKNOWN |
| Verdict | NOT_ACCEPTED |
| Evidence reference | No accepted QA task for current main SHA was supplied or found in repository evidence |
| Merge allowed | false |
| Tested at | UNKNOWN |

### QA capabilities

No capability assertions recorded.

### QA provenance

- `NOT_VERIFIED` / `qa_task` — Current main SHA has no canonical accepted QA task in this status snapshot (observed `2026-09-29T09:24:27Z`)

## PRODUCTION

| Field | Value |
|---|---|
| State | PARTIALLY_VERIFIED |
| Runtime SHA | UNKNOWN |
| Operational DB revision | UNKNOWN |
| Public DB revision | UNKNOWN |
| Active release ID | public-v1-20260927T040214Z-32334077-8f44d69a |
| Active release record count | 40 |
| Active release cohort | UNKNOWN |
| Semantic-ready count | UNKNOWN |
| Public-ready count | UNKNOWN |
| Service state | PARTIALLY_VERIFIED |
| Last production acceptance task | UNKNOWN |
| Deployed at | UNKNOWN |

### PRODUCTION capabilities

No capability assertions recorded.

### PRODUCTION provenance

- `VERIFIED` / `runtime_observation` — https://nextcompany.pro/api/ready returned ready with release_id and record_count=40 (observed `2026-09-29T09:24:19Z`)
- `VERIFIED` / `runtime_observation` — https://nextcompany.pro/api/health returned status=ok (observed `2026-09-29T09:24:19Z`)
- `NOT_VERIFIED` / `production_handoff` — Runtime SHA, DB revisions, deployment time, readiness counters, and production acceptance task are not exposed by the observed public endpoints (observed `2026-09-29T09:24:27Z`)

## USER_VISIBLE

| Field | Value |
|---|---|
| State | PARTIALLY_VERIFIED |
| Public site | LIVE |
| External API | LIVE |
| SSR | VERIFIED |
| Active release ID | public-v1-20260927T040214Z-32334077-8f44d69a |
| Externally verified at | 2026-09-29T09:24:19Z |
| Withheld/depublished knowledge | PARTIALLY_VERIFIED |

### USER_VISIBLE capabilities

| Capability | Status | Evidence |
|---|---|---|
| `public-company-card` | `VISIBLE` | SSR and JSON API for company 0100000614 returned HTTP 200 with canonical and structured data |

### Known withheld or depublished companies

| Identifier | State | Reason |
|---|---|---|
| `0100000614` | `WITHHELD` | External SSR response was HTTP 200 but carried noindex; indexing eligibility for the other active records was not exhaustively checked |

### Known user-visible issues

- `EMPTY_PUBLIC_SITEMAP` — The public sitemap returned an empty urlset at external verification time

### USER_VISIBLE provenance

- `VERIFIED` / `runtime_observation` — Public landing, API ready/health, sample company API, and sample SSR card returned HTTP 200; www redirected to apex (observed `2026-09-29T09:24:19Z`)

## Active task and next gate

| Field | Value |
|---|---|
| Active task | MAC-OFFLINE-WORKSPACE-PRODUCT-COMPLETION-03D-USERS-ROLES-SETTINGS-01 |
| Active task state | COMPLETE |
| Active task title | NEXT COMPANY — USERS / ROLES / WORKSPACE SETTINGS / USAGE P0 |
| Next gate | MAC-OFFLINE-WORKSPACE-PRODUCT-COMPLETION-03D-USERS-ROLES-SETTINGS-01-QA |
| Next gate state | READY |
| Next gate description | Independent QA and exact-head CI for Workspace Users, Roles, Settings and Usage P0 |

## Incidents

State: `NOT_VERIFIED`.

No active incidents are listed. The section state controls whether that is verified.

## Services

| Service | Environment | State | Evidence |
|---|---|---|---|
| `public-web` | `production` | `HEALTHY` | https://nextcompany.pro/ and sample SSR company card returned HTTP 200 |
| `public-api` | `production` | `HEALTHY` | /api/health, /api/ready, and sample company API returned HTTP 200 |
| `operational-worker` | `production` | `NOT_VERIFIED` | Internal operational worker state was not supplied and is not exposed publicly |

## Data state

| Field | Value |
|---|---|
| Operational DB revision | UNKNOWN |
| Public DB revision | UNKNOWN |
| Active release ID | public-v1-20260927T040214Z-32334077-8f44d69a |
| Active release record count | 40 |
| Active release cohort | UNKNOWN |
| Semantic-ready count | UNKNOWN |
| Public-ready count | UNKNOWN |

### Data provenance

- `VERIFIED` / `runtime_observation` — /api/ready exposed active release_id and record_count=40 (observed `2026-09-29T09:24:19Z`)
- `NOT_VERIFIED` / `production_handoff` — DB revisions, exact active cohort identity, and readiness counters were not supplied by the runtime observation (observed `2026-09-29T09:24:27Z`)

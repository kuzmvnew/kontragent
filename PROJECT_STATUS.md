# PROJECT STATUS — GENERATED — DO NOT EDIT MANUALLY

> Canonical source: [`project_status.yaml`](project_status.yaml).
> Regenerate with `uv run python scripts/project_status.py update`.

- Schema version: `1.0.0`
- Generated at: `2026-10-10T07:52:31Z`
- Canonical main SHA: `c9bdb49768dc503af8ec82bdaa97653bc7412f3b`

Layer states are independent. A merge is not deployment, QA acceptance is
not deployment, and production deployment is not user-visible verification.

## CODE

| Field | Value |
|---|---|
| State | VERIFIED |
| Main SHA | c9bdb49768dc503af8ec82bdaa97653bc7412f3b |
| Latest relevant merged PR | #140 — https://github.com/kuzmvnew/kontragent/pull/140 |
| Merge SHA | c9bdb49768dc503af8ec82bdaa97653bc7412f3b |
| Merged at | 2026-10-09T06:43:51Z |
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
| `workspace-users-roles-settings` | `MERGED` | PR #138 / merge 64bb84a1e34bc4173ea1dc26daef2bfdfb87b2f0 |
| `workspace-demo-full-e2e` | `MERGED` | PR #139 / merge 4256f6717047e664a775d82ecd8405ed2b4025a8 |
| `workspace-visual-ux-polish-04a-core-shell-card` | `MERGED` | PR #140 https://github.com/kuzmvnew/kontragent/pull/140 merged as c9bdb49768dc503af8ec82bdaa97653bc7412f3b |
| `real-data-local-preview-alan-01` | `NOT_MERGED` | PR #141 https://github.com/kuzmvnew/kontragent/pull/141 is OPEN at exact head 653d46f4bc6f144cb0190e9a1995dd45cbce7245 |

## QA

| Field | Value |
|---|---|
| State | NOT_ACCEPTED |
| Accepted head SHA | UNKNOWN |
| QA task ID | UNKNOWN |
| Verdict | NOT_ACCEPTED |
| Evidence reference | PR #141 exact head 653d46f4bc6f144cb0190e9a1995dd45cbce7245 awaits trusted independent QA acceptance |
| Merge allowed | false |
| Tested at | UNKNOWN |

### QA capabilities

| Capability | Status | Evidence |
|---|---|---|
| `workspace-core-03a` | `ACCEPTED` | Functional product completion 03A was supplied as a DONE / QA ACCEPTED baseline for 04A |
| `workspace-reports-export-03b` | `ACCEPTED` | Functional product completion 03B was supplied as a DONE / QA ACCEPTED baseline for 04A |
| `workspace-bulk-check-03c` | `ACCEPTED` | Functional product completion 03C was supplied as a DONE / QA ACCEPTED baseline for 04A |
| `workspace-users-settings-03d` | `ACCEPTED` | Functional product completion 03D was supplied as a DONE / QA ACCEPTED baseline for 04A |
| `workspace-demo-full-e2e-03e` | `ACCEPTED` | Functional product completion 03E was supplied as a DONE / QA ACCEPTED baseline for 04A |
| `workspace-visual-ux-polish-04a` | `ACCEPTED` | PR #140 trusted QA PASS for exact head 00c25c17cda6c71481075369fbfde808bb933c2c: https://github.com/kuzmvnew/kontragent/pull/140#issuecomment-6075820438 |
| `real-data-local-preview-alan-01` | `NOT_ACCEPTED` | PR #141 EV-01 normalized provenance and EV-02 Report contract corrections require trusted independent exact-head QA; no attestation is published for 653d46f4bc6f144cb0190e9a1995dd45cbce7245 |

### QA provenance

- `NOT_VERIFIED` / `qa_task` — https://github.com/kuzmvnew/kontragent/actions/runs/38032203968 reports NO_TRUSTED_ATTESTATION for open PR #141 exact head 653d46f4bc6f144cb0190e9a1995dd45cbce7245 (observed `2026-10-10T07:52:31Z`)

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
| Active task | REAL-DATA-LOCAL-PREVIEW-ALAN-01-CORRECTION-02 |
| Active task state | COMPLETE |
| Active task title | Guarded Alan Real Data Preview provenance and Report contract correction |
| Next gate | REAL-DATA-LOCAL-PREVIEW-ALAN-01-CORRECTION-02-QA |
| Next gate state | READY |
| Next gate description | Independent exact-head QA of EV-01 provenance evidence, EV-02 real Chromium E2E, and regression results |

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

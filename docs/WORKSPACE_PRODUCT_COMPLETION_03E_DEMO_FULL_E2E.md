# Workspace Product Completion 03E — Demo 1.0 and full E2E

Task: `MAC-OFFLINE-WORKSPACE-PRODUCT-COMPLETION-03E-DEMO-FULL-E2E-01`

## Architecture verdict

Demo 1.0 is orchestration around the existing product, not a parallel app.

```text
synthetic cohort
  ├─ operational PostgreSQL
  │    ├─ Company + CompanySemanticFact
  │    └─ real Workspace services and tenant-owned rows
  └─ deterministic PublicProjection bundle
       └─ import_release (validate → stage → accept → promote)
            └─ active public release tables
                 └─ read-only PublicRepository
                      ├─ Public FastAPI
                      └─ Workspace FastAPI
```

The runtime imports no `tests.*`, fake projection repository, browser test
repository, or test projection helper. Demo mode changes only visible safety
signalling and Public noindex behavior. It does not auto-login, disable CSRF,
grant permissions, change tenant scope, replace `PublicRepository`, or change
Company View rules.

## Database topology and safety

`scripts/workspace_demo_support.py` accepts only PostgreSQL URLs on
`localhost`/`127.0.0.1`, requires `demo` in both database names, and requires
separate operational/public databases. There is no production override.

Bootstrap verifies migration heads and refuses stale or unknown schemas. Reset
requires `LOCAL_DEMO_ONLY` and refuses a public database containing any release
outside the `nextcompany-demo-` namespace.

## Demo data and provenance

The deterministic cohort contains six valid 10-digit legal-entity INNs with
names beginning `СИНТЕТИЧЕСКАЯ ДЕМО`. These identifiers are local fixtures and
must not be interpreted as claims about actual entities.

Every public projection has `index_eligible=false`. Demo mode also forces:

- `X-Robots-Tag: noindex, nofollow, nosnippet`
- `Cache-Control: no-store`
- empty sitemap results
- a persistent banner in Public and Workspace

Company View facts use:

- source name: `Демонстрационный набор next.company`
- source class: `DEMO_SYNTHETIC`

The required `REVEXP`, `PAYTAX`, `DEBTAM`, and `TAXOFFENCE` blocks remain in the
unchanged public contract but use `NOT_CHECKED`, no values, no source data date,
and an explicit statement that no official source was queried. Risk and Summary
remain limited, explicitly synthetic, and never expose a numeric NEXT Index.

## Public release path

The bundle is deterministic and contains `manifest.json`,
`companies.jsonl.gz`, and `checksums.sha256`. It is loaded by the ordinary bundle
validator and activated only through `scripts.import_public_release.import_release`.
Manifest validation, payload hashes, record counts and atomic active release
switching remain intact.

Bundle provenance is resolved from the checked-out `git HEAD`; no fallback SHA
is embedded in source. `NEXTCOMPANY_DEMO_EXPECTED_SHA`, when present, must be a
canonical lowercase 40-character SHA and must equal the actual checkout. Both
`source_main_sha` and `cohort_source_main_sha` use that value. Reusing the stable
Demo release ID with content from another SHA fails closed and requires reset.

Web reads use `PUBLIC_DATABASE_URL`; imports use
`PUBLIC_IMPORT_DATABASE_URL`. Hosted acceptance provisions a separate web role,
proves `SELECT`, and proves mutation of `public_publication_state` is denied.

## CLEAN and SHOWCASE

CLEAN creates the Demo cohort, owner, Workspace and explicit Demo entitlements,
with no customer-generated product rows. Repeated CLEAN bootstrap creates no
duplicates. Reset followed by CLEAN produces the same logical state.

SHOWCASE calls production service functions to create:

- Saved companies and a persisted note;
- a monitoring subscription and baseline;
- a semantic fact transition through `monitor_company_once`, producing a real
  `MonitoringEvent` and `WorkspaceFeedEntry`;
- a historical report through `generate_report` and real `PublicRepository`;
- a completed Bulk job through `create_bulk_job` and
  `process_bulk_job_chunk`, pinned to the active public release;
- a representative member through invitation creation and acceptance.

Monitoring is enabled by bootstrap/system authority on this Demo Workspace only;
the normal Workspace default remains unchanged.

## Full browser acceptance

`tests/test_workspace_demo_full_e2e.py` starts both real FastAPI apps and drives
real Chromium across:

- Public search and Public Card;
- Public-to-Workspace return path, login, session and CSRF;
- dashboard and all eight navigation destinations;
- authorized search/card identity;
- save, note, monitoring, event, mark-read, pause and resume;
- report creation, history, JSON and CSV;
- Bulk READY, DUPLICATE and INVALID_INN states plus exports;
- invitation, clean-context acceptance, immediate role change and server-side
  MEMBER denial;
- Workspace rename, Usage versus independent DB truth, logout and protected
  route redirect.

The gate asserts action-to-row persistence for Saved, Monitoring, feed, Report,
Bulk, Invitation/Membership, role and Settings. Screenshots and `report.json`
contain no password, invitation token, session cookie, CSRF token, or database
credential.

## Hosted gate

`.github/workflows/workspace-demo-e2e.yml` uses PostgreSQL 18.6, fresh
operational/public Demo databases, both migrations, a read-only Public web role,
the real release importer, and mandatory Chromium. The critical Demo tests are
not skipped in that job. Pull-request runs explicitly checkout the PR head SHA,
verify it before bootstrap, and expose it only as an expected-SHA assertion.
The Demo owner password is generated at runtime with `secrets.token_urlsafe`,
masked immediately and passed only through `GITHUB_ENV`; no repository default
exists. The acceptance report asserts alignment between actual/expected HEAD,
manifest source/cohort provenance, the public release row and `report.git_sha`.

## Evidence boundary

Passing Demo acceptance means **functional Demo path accepted by code/tests**.
It does not mean:

- SOURCE VERIFIED;
- production data accepted;
- public production release accepted;
- production mutated or deployed;
- SEO 10K readiness achieved.

`PRODUCTION_MUTATION=NO`, `HOME_REQUIRED=NO`, `DEPLOY=NO`,
`MASS_INGESTION=NO`, `SOURCE_LIVE=NO`, and
`PUBLIC_PRODUCTION_RELEASE=NO` remain explicit.

## Known gaps

- HOME controlled-live C6 remains `READY_FOR_HOME_CONTROLLED_LIVE` /
  `WAIT_HOME_18_OCT`.
- Production deployment, domain/certificate rollout and production scheduler.
- Real invitation email delivery.
- Production mass ingestion and the 10K public SEO release.
- Further visual polish beyond the persistent compact Demo banner.

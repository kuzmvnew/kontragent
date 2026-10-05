# MAC-OFFLINE-WAVE-B-WORKSPACE-P0-VERTICAL-SLICE-02

Status: READY FOR QA after exact-head CI

Base: `773c0e49d1bb38c926227d659349541c2ec41dec`

Branch: `mac/workspace-p0-vertical-slice-02`

Production mutation: NO

## Current-state audit

### FACT

- The accepted foundation already supplied durable customer sessions, active
  Workspace selection, server-side RBAC and entitlements, tenant-scoped
  `SavedCompany`, audit writes, quota serialization and CSRF.
- Public and private company reads both use `PublicRepository` and the accepted
  `PublicProjection`/Company View contract. This slice introduces no provider,
  RAW, parser, or Workspace-specific company representation.
- On the base commit, the public card had no customer entry action, the public
  header had no customer login, and multi-Workspace login discarded its
  intended destination during Workspace selection.
- The base Workspace UI exposed the routes but the authorized card reduced
  Company View to section counts; Saved Companies had no inline removal; no
  honest Monitoring transition existed.

### TESTED

- Public card → login → intended authorized company card works with one active
  Workspace.
- With two active Workspaces, safe `return_to` survives login and validated
  Workspace selection; arbitrary, protocol-relative, credential-bearing and
  path-bearing origins are rejected.
- Search by INN/name uses `PublicRepository`; authorized HTML and private API
  use the same projection and `_card_context` action state.
- Save, duplicate-safe server state, quota/permission/entitlement denial,
  Saved list, open card, Unsave, audit, disabled membership and tenant
  isolation remain server-authoritative.
- Real Playwright Chromium acceptance covers both the primary journey and the
  required two-Workspace isolation journey without production sources.

### GAP / limitation

- Monitoring subscription, change detection, event persistence and Workspace
  feed are not implemented. The stable entry route reports `NOT_ACTIVE` when
  authorization or entitlement is absent and `NOT_IMPLEMENTED` when both
  gates allow the future action.
- There is no public signup, invitation, password reset, billing integration,
  analytics dashboard or fake KPI.
- Authorized audience expansion is not attempted. The card uses the already
  minimized accepted public semantic projection and does not expose RAW or
  private provider fields.

## Route map

| Surface | Route | Contract |
| --- | --- | --- |
| Public header | `GET /...` | `Войти` links to canonical customer login |
| Public company | `GET /companies/{inn}` | `Открыть в кабинете` carries `/app/companies/{inn}` as safe `return_to` |
| Customer login | `GET/POST /login` | pre-session CSRF; customer cookie only; no signup |
| Workspace selection | `GET/POST /workspace/select` | lists active memberships; preserves only a validated `/app...` destination |
| Workspace home | `GET /app` | Workspace name, role, user, Saved count, Search and Saved entry |
| Search | `GET /app/search?q=...` | accepted repository by exact INN or supported name query |
| Authorized card | `GET /app/companies/{inn}` | accepted semantic projection plus real Workspace action state |
| Save/Unsave | `POST /app/companies/{inn}/save|unsave` | active user → membership → permission → entitlement → quota → tenant scope |
| Saved list | `GET /app/saved` | active Workspace only; open and authorized Unsave actions |
| Monitoring transition | `GET /app/companies/{inn}/monitoring` | saved-company prerequisite; honest `NOT_ACTIVE`/`NOT_IMPLEMENTED` state |
| Private API | `/app/api/...` | same repository and action-state helpers as SSR |

## User journey

1. A visitor opens an accepted public company card.
2. `Открыть в кабинете` sends the visitor to customer login with
   `/app/companies/{inn}` as the destination.
3. Login selects the single active Workspace automatically, or asks the user
   to choose among validated active memberships while retaining the
   destination.
4. The authorized card shows the active Workspace, Company View domains,
   source state/freshness, Risk, Summary and limitations.
5. Workspace Search finds the same accepted projection by INN or supported
   company-name query.
6. Save runs only through the accepted server-side service. The redirected
   card renders the updated state and a clear success notice.
7. Saved Companies opens the same authorized card and supports authorized
   removal. It never creates a Saved-specific card.
8. A saved card exposes Monitoring status without creating a subscription or
   claiming that monitoring is active.

## Origin and navigation contract

Same-origin routing requires no host configuration. If public and Workspace
apps are deployed on separate origins, the boundary is explicit and validated:

- public app: `WORKSPACE_ORIGIN=https://cabinet.example`;
- Workspace app: existing `PUBLIC_ORIGIN=https://public.example`.

Both values accept only an HTTP(S) origin with no credentials, path, query or
fragment. Hosts are not hardcoded in templates. `return_to` remains a relative
path and `safe_return_to` accepts only `/app...`; an origin is never trusted as
a post-login destination.

## Company View and HTML/API parity

The authorized HTML card and `GET /app/api/companies/{inn}` resolve the same
`PublicProjection` and the same `_card_context`. The card renders identity,
status, address, activity, management, founders, finance, employees, tax,
enforcement, licenses, courts, bankruptcy, procurement/RNP, restrictions,
inspections and connections whenever those accepted sections are present. A
missing or unchecked domain remains explicit; the UI does not manufacture a
positive result.

The API and SSR expose the same `is_saved`, Save availability/quota and
Monitoring transition state. Internal company/workspace IDs, RAW payloads,
credentials, parser metadata and filesystem paths are outside the contract.

## Save and Saved Companies

All writes reuse the foundation service and the global `Company` row. The
server performs membership, permission, entitlement, quota and Workspace
scope checks; client state is never authority. PostgreSQL uniqueness preserves
idempotency and successful/denied outcomes remain audited. HTML translates
denials into user-facing copy while the private API keeps stable machine codes.

Saved list rows expose only company name, INN, saved date and optional note.
Opening a row uses `/app/companies/{inn}`. Unsave posts with CSRF and can return
to the Saved list, whose empty state links back to Search.

## Monitoring transition contract

The entry is displayed only after Save and resolves the current Workspace
through the existing session. It checks `monitoring.manage` and
`monitoring.enabled` through `action_state`:

- missing permission or disabled entitlement: `NOT_ACTIVE` with human copy;
- permission and entitlement allowed: `NOT_IMPLEMENTED`;
- unsaved company: entry route refuses the transition.

No subscription, fake event, feed, scheduler or activation audit is created by
this task.

## Test evidence

- Focused Workspace + public route + browser suite: `34 passed`.
- Company View/public projection regression: `77 passed, 3 skipped`; the
  skips are provider/database opt-ins and are not Workspace P0 cases.
- Browser E2E: `2 passed` (single-Workspace Save/Unsave and two-Workspace
  selection/isolation).
- S02 sanity regression: `178 passed` on a fresh migrated database.
- Full repository suite on a fresh database: `1698 passed, 20 skipped, 3
  failed`. Workspace P0 skips are zero. The three failures are the unchanged,
  previously documented `tests/test_firmoteka_scale.py` baseline failures
  (`test_active_crawl_applies_new_lane_config_between_jobs`,
  `test_backpressure_claims_company_batch_once_with_job_identity`, and
  `test_failed_checkpoint_is_reclaimed_with_new_idempotency_generation`).
  No Firmoteka implementation or test file is changed by this branch.
- Hosted exact-head CI is recorded in the PR/QA handoff.

The main CI installs the pinned Playwright Chromium runtime before the full
suite, so the Workspace browser acceptance is mandatory and cannot silently
become an optional skip.

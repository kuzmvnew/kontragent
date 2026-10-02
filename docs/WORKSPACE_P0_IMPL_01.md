# MAC-OFFLINE-WAVE-B-WORKSPACE-P0-FOUNDATION-01

Status: READY FOR QA (exact SHA required before merge)

Base: `e4f12d7878a3c5aa844cbb19dcf3941f818aa509`

Branch: `mac/workspace-p0-foundation-01`

Production mutation: NO

## Current-state audit

### FACT

- `Company.id` is the canonical global company identity (`companies.id`,
  `BIGINT`). `companies.inn` is globally unique. No tenant copy of Company
  existed on the base commit and none is introduced here.
- Operational persistence uses SQLAlchemy sessions from
  `app.database.postgres`; Alembic metadata is registered through
  `app.models`.
- The base customer/public surfaces are separate from `admin_app`. The admin
  console has its own `nextcompany_admin_session`, scrypt password primitive,
  process-local allow-list and admin audit model.
- The base commit had no customer User, Workspace, Membership, customer
  session, permission, entitlement or Saved Company model.
- Public company delivery uses accepted projections derived from semantic
  company contracts. The canonical semantic contract is
  `CompanyViewModelV1`; Workspace code does not read provider RAW payloads.
- The base migration head was `b9e2c4d6f8a0`.

### TESTED

- Fresh PostgreSQL install upgraded from an empty database to
  `d3e5f7a9b1c4`.
- `d3e5f7a9b1c4 -> b9e2c4d6f8a0` downgrade and re-upgrade both complete.
- Schema completeness is compatible: no missing/incompatible model objects.
- Focused real-PostgreSQL auth, tenant, RBAC, entitlement, CSRF and Saved
  Company tests pass.
- Legacy-reconciliation/current-head guards pass after advancing the canonical
  head.

### GAP on the base commit

- No durable customer authentication or revocation.
- No server-side tenant boundary.
- No customer RBAC or tariff-entitlement boundary.
- No workspace-scoped Saved Company lifecycle.
- No private `/app/api/...` foundation.

## Architecture decisions

1. Customer auth is a separate boundary from admin auth. Cookie names, session
   persistence and authorization code are separate.
2. Customer sessions are opaque 256-bit tokens; PostgreSQL stores only their
   SHA-256 digests. Session authority survives process restart and works across
   processes.
3. Every protected action evaluates, in order: active user, active Workspace,
   active membership, role-owned permission, independently named entitlement,
   quota where applicable, and workspace resource scope.
4. Permissions and entitlements have disjoint key spaces. For example,
   `company.save` maps to `saved_companies.enabled`.
5. Saved Company references the global `companies.id`. Company master rows are
   never copied into Workspace storage.
6. Browser writes use session-bound double-submit CSRF tokens. Cookies are
   HttpOnly, SameSite=Strict and automatically Secure outside local/dev/test.
7. Private responses expose INN and semantic projection data, not internal
   `company_id`, workspace IDs, RAW payloads or password hashes.
8. Login, active-Workspace selection and logout write canonical audit actions
   in the same transaction as the session mutation. An audit insert failure
   therefore rolls back session creation, selection or revocation.

## Data model

| Domain | Table | Main invariants |
| --- | --- | --- |
| User | `customer_users` | UUID, normalized unique email, scrypt hash, active/disabled |
| Workspace | `workspaces` | UUID, name, active/suspended/closed |
| Role | `workspace_roles` | tenant-scoped `OWNER`/`ADMIN`/`MEMBER` |
| Permission | `workspace_role_capabilities` | role + permission key |
| Membership | `workspace_memberships` | unique workspace/user; composite FK prevents a role from another tenant |
| Entitlement | `workspace_entitlements` | unique workspace/entitlement key; optional non-negative limit |
| Session | `customer_sessions` | token hash, CSRF hash, expiry, revocation, active Workspace |
| Saved Company | `saved_companies` | workspace + global company FK unique; saving user retained |
| Audit | `workspace_audit_events` | workspace/actor/action/target/outcome; narrowly nullable before tenant or principal resolution |

## Authentication contract

- Email/password login; passwords require versioned stdlib scrypt hashes.
- Login uses a pre-session CSRF token and generic invalid-credential response.
- Active Workspace is selected automatically only when exactly one active
  membership exists.
- Canonical audit actions are `auth.login`, `workspace.select` and
  `auth.logout`; their outcomes are `success` or `denied`.
- A multi-membership login is audited without a Workspace until the user makes
  a validated selection. Failed pre-auth login is never assigned to a
  Workspace. An unresolved principal uses a bounded SHA-256 identity reference
  and a null actor; raw email and credential/session material are not stored.
- A denied Workspace selection is recorded with the authenticated actor and a
  null Workspace, so an untrusted requested tenant is never attributed.
- Each request resolves the opaque cookie against an unexpired, unrevoked
  PostgreSQL row and rechecks the active user.
- Logout revokes the database session before deleting browser cookies.
- Disabled users fail authentication; disabled memberships fail authorization.
- Customer cookie: `nextcompany_session`; admin cookie:
  `nextcompany_admin_session`.

## RBAC contract

Permissions:

- `workspace.view`
- `company.search`
- `company.view`
- `company.save`
- `company.unsave`
- `monitoring.manage`
- `workspace.members.manage`

`OWNER` and `ADMIN` receive all P0 permissions. `MEMBER` receives Workspace
view plus company search/view/save/unsave. The database prevents a membership
from referencing a role owned by another Workspace.

## Entitlement contract

Permission-to-entitlement mapping:

| Permission | Entitlement |
| --- | --- |
| `workspace.view`, `company.search`, `company.view` | `workspace.core.enabled` |
| `company.save`, `company.unsave` | `saved_companies.enabled` |
| `monitoring.manage` | `monitoring.enabled` |
| `workspace.members.manage` | `workspace_members.enabled` |

The Saved Company quota lives only on `saved_companies.enabled`. Save locks the
entitlement row before counting and inserts with PostgreSQL
`ON CONFLICT DO NOTHING`, making quota decisions serialized per Workspace and
duplicate saves idempotent.

## Tenant-isolation invariants

- A requested Workspace is never trusted without an active membership query.
- The selected role must belong to the same Workspace (runtime check plus
  composite foreign key).
- Saved Company reads include both resource ID and `workspace_id`.
- List/save/unsave always scope by the authorized Workspace.
- A global company lookup resolves one canonical Company; no Workspace Company
  row is created.
- Missing user, membership, permission, entitlement or resource scope fails
  closed.

## Migration

Revision: `d3e5f7a9b1c4`

Parent: `b9e2c4d6f8a0`

Single Alembic head: yes

Downgrade: implemented and tested

Fresh database: tested

Schema completeness: compatible

## API

Canonical private namespace:

- `GET /app/api/login/csrf`
- `POST /app/api/login`
- `GET /app/api/context`
- `GET /app/api/csrf`
- `POST /app/api/logout`
- `GET /app/api/companies/{inn}`
- `GET /app/api/saved-companies`
- `POST /app/api/companies/{inn}/saved`
- `DELETE /app/api/companies/{inn}/saved`

The minimal server-rendered routes remain only as an acceptance surface. The
authorized card reuses an accepted semantic projection and the Save operation
resolves the exact global Company row before writing.

## Verification

- Focused Workspace PostgreSQL suite: `16 passed`, including HTML/API audit
  semantics and audit-insert rollback for login/select/logout.
- Current-head/legacy-reconciliation targeted suite: `24 passed`.
- Fresh install, previous-head upgrade, downgrade/re-upgrade, one-head and
  schema-completeness checks pass; schema report is `compatible: true`.
- Full repository suite result in this local environment: `1685 passed, 20
  skipped, 3 failed`. The three failures are the pre-existing
  `tests/test_firmoteka_scale.py` cases and reproduce with both Firmoteka code
  and tests unchanged from `bda08a6e`; they are outside this correction's
  authorized scope.

## Limitations

- No public self-registration, invitations, password reset or external IdP.
- No billing provider or commercial tariff acceptance.
- No native PostgreSQL RLS; isolation is enforced through constraints and the
  server-side service boundary.
- No distributed login rate-limit service yet.
- Monitoring and member-management permissions/entitlements are foundations;
  their product flows are not implemented in P0.
- Workspace Search/Card/Saved UI remains intentionally minimal.
- No production deployment, production migration or production user creation
  was performed.

HOME is not required for the next local QA gate. It will be required only for
a separately authorized deployment gate.

# WORKSPACE-P0-IMPL-01

**TASK:** MAC-OFFLINE-WAVE-B-WORKSPACE-P0-01  
**Status:** IMPLEMENTATION CANDIDATE / QA PENDING  
**Production:** NOT DEPLOYED

## Scope

Workspace P0 implements the controlled/private vertical slice:

```text
Login
  ↓
Workspace membership
  ↓
Search accepted company projections
  ↓
Authorized Company Card
  ↓
Save
  ↓
Saved Companies
```

Public registration, billing-provider integration, custom roles, invitations,
monitoring delivery, bulk checks and P1/P2 management screens are deliberately
outside this slice.

## Boundaries

- `Company.id` remains the one global company identity.
- Workspace-owned objects store `workspace_id` and a foreign key to the
  global Company; company master data is not copied into tenant tables.
- Customer auth is a separate `workspace_app`; it does not reuse
  `admin_app.auth` or owner-console sessions.
- Company display data comes from the accepted `PublicProjection` contract.
  Operational `company_id` is used only server-side for Save/tenant links.
- Private HTML/API are `noindex, nofollow, nosnippet` and `no-store`.
- The browser never receives internal `company_id`.
- No RAW/parser/worker objects are exposed.

## Persistence

New operational tables:

- `customer_users`
- `workspaces`
- `workspace_roles`
- `workspace_role_capabilities`
- `workspace_memberships`
- `customer_sessions`
- `workspace_entitlements`
- `saved_companies`
- `workspace_audit_events`

Customer sessions are opaque random tokens. Only SHA-256 hashes of session and
CSRF tokens are stored in PostgreSQL. Sessions have expiry and explicit
revocation.

## Authorization

Every protected action is evaluated server-side. P0 uses:

1. active customer user;
2. active Workspace;
3. active Workspace membership;
4. role capability;
5. enabled Workspace entitlement;
6. quota when the capability has a quantitative limit;
7. exact tenant resource scope;
8. effect + audit.

P0 capability keys:

- `workspace.read`
- `company.search`
- `company.read`
- `company.save`

Permission and entitlement are independent. A missing/disabled entitlement is
fail-closed and distinct from a missing capability.

## Quota

`company.save` may carry `limit_value`.

Save locks the entitlement row before checking the current Workspace saved
count. This serializes concurrent quota decisions for the same Workspace.
Already-saved companies are idempotent and do not consume another unit.

The controlled bootstrap requires an explicit saved-company limit; no
commercial tariff number is inferred from UI references.

## Authentication / CSRF

- Password hashes use versioned stdlib scrypt.
- Login creates a fresh opaque DB-backed session.
- POST actions require a session-bound CSRF value.
- Session revoke immediately blocks private routes.
- `return_to` accepts only internal `/app...` paths.
- There is no public signup endpoint in P0.

The bootstrap command is:

```bash
uv run python scripts/bootstrap_workspace_owner.py \
  --email owner@example.test \
  --workspace-name "NEXT Workspace" \
  --saved-limit 100
```

The password is prompted without echo unless explicitly supplied by the
operator. This command is controlled environment provisioning, not customer
self-registration.

## API / UI

Implemented private routes:

- `GET/POST /login`
- `POST /logout`
- `GET/POST /workspace/select`
- `GET /app`
- `GET /app/search`
- `GET /app/companies/{inn}`
- `POST /app/companies/{inn}/save`
- `POST /app/companies/{inn}/unsave`
- `GET /app/saved`
- `GET /api/app/companies/{inn}`
- `GET /api/app/saved`

The authorized company response reuses the accepted public semantic projection
and adds only safe action context such as `is_saved`, `can_save` and quota
summary. It does not serialize internal company/workspace database identifiers.

## Test contract

`tests/test_workspace_p0.py` proves on real PostgreSQL:

- login and DB-backed session;
- CSRF rejection;
- explicit session revocation;
- one-workspace auto selection;
- tenant A cannot select/access tenant B;
- Search → Authorized Card → Save → Saved;
- idempotent/scope-safe Saved Company model;
- permission, entitlement and quota are distinct;
- save quota is enforced;
- audit contains success and quota rejection;
- private API/HTML do not leak `company_id`, RAW/parser/worker markers.

## Not claimed

- production deployment;
- production customer identity acceptance;
- external IdP/SSO;
- native PostgreSQL RLS acceptance;
- commercial tariff acceptance;
- billing/payment acceptance;
- public signup;
- monitoring/scheduler acceptance;
- HOME execution.

This slice is intended to reach **QA ACCEPTED / READY_FOR_HOME_DEPLOY**, not
to claim production readiness by code alone.

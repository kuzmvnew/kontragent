# Workspace 03D — Users, Roles, Settings and Usage

Task: `MAC-OFFLINE-WORKSPACE-PRODUCT-COMPLETION-03D-USERS-ROLES-SETTINGS-01`

## Identity and membership

`CustomerUser` remains the global customer identity. A user's access to a
Workspace is represented only by `WorkspaceMembership`; no tenant-local user
or parallel authorization table is introduced. `WorkspaceRole` and persisted
`WorkspaceRoleCapability` rows define authority in that Workspace.

The only supported roles in 03D are the existing system roles:

- `OWNER` — full Workspace administration, including authority over owners.
- `ADMIN` — may manage `ADMIN` and `MEMBER`, but cannot grant or alter `OWNER`.
- `MEMBER` — product use and team visibility through `workspace.view`, without
  member or settings administration.

The Users page renders the capability matrix from
`workspace_role_capabilities`; it does not hard-code a second description of
permissions and does not permit capability editing.

## Capability and entitlement contract

`OWNER` and `ADMIN` receive:

- `workspace.members.manage` → `workspace.core.enabled`
- `workspace.members.invite` → `workspace_members.enabled`
- `workspace.settings.manage` → `workspace.core.enabled`

The split is intentional. Disabling the commercial member entitlement blocks
new invitations and reactivation that consumes a seat, but it never blocks
suspension, revocation, role reduction, invitation revocation, or other access
cleanup. Migration backfill and fresh Workspace bootstrap use the same role
capability contract.

`workspace_members.enabled.limit_value` is the seat limit. `NULL` means
unlimited; there is no invented default. Capacity is:

`active memberships + unexpired pending invitations`

Invitation creation and reissue lock the entitlement row before counting and
reserving capacity. Reactivation locks the same row. Concurrent growth actions
therefore cannot exceed a configured limit. Expired and revoked invitations do
not consume capacity, and accepting a pending invitation replaces its existing
reservation rather than consuming a second seat.

## Invitation lifecycle and delivery

`WorkspaceInvitation` is durable and tenant scoped. It stores the normalized
email, Workspace role, inviter, status, finite expiry, acceptance/revocation
timestamps, and only a SHA-256 token fingerprint. A composite foreign key
ensures the invitation role belongs to the invitation Workspace. A partial
unique index permits only one `PENDING` invitation per Workspace/email.

Invitation states are `PENDING`, `ACCEPTED`, and `REVOKED`. A pending row whose
`expires_at` has passed is presented as expired and is unusable without a
scheduler. Reissue revokes the old row and creates a fresh seven-day token;
the old link stops working. Accepted, revoked, expired, and superseded tokens
all fail through the same safe invalid/expired boundary.

Raw tokens are generated with a cryptographically secure 256-bit source. The
raw invitation link is returned to the manager exactly once after create or
reissue. It is never written to the database, audit events, logs, documentation,
or project status. External email delivery is not part of 03D; the manager
copies the one-time link and sends it manually.

The canonical email normalizer rejects raw Unicode control (`Cc`), format
(`Cf`), and surrogate (`Cs`) characters before trimming or casefolding. This
includes NUL, DEL, C1 controls, zero-width format characters, and BOM. Valid
emails retain the existing whitespace trim, casefold, length, and simple shape
contract. Invalid invitation input returns `invalid_email` (400) before any
invitation, seat reservation, or success audit is written.

## Acceptance

`GET /invite/{token}` validates the invitation and issues a dedicated,
short-lived CSRF cookie. `POST /invite/{token}` requires that CSRF value in
addition to the invitation token.

For a new email, acceptance enforces the existing minimum 14-character policy,
uses `hash_password()` (`scrypt-v1`), creates `CustomerUser`, and creates or
reactivates the exact invited membership and role. For an existing user, the
current password is required and the stored password is not modified. Disabled
users cannot accept.

The invitation row is locked during acceptance. User/membership creation,
single-use transition, audit event, and normal customer session creation occur
in one database transaction. The session starts with the invited Workspace
active and the browser is redirected to `/app`. Concurrent acceptance produces
one identity, one membership, and one accepted transition; the other attempt
receives the safe invalid/expired result rather than a database error.

## Member state and owner safety

Membership state transitions preserve the membership identifier and history:

- `active → suspended`
- `active|suspended → revoked`
- `suspended|revoked → active` (subject to active user, entitlement, and quota)

The management surface cannot suspend or revoke the current user's own
membership. An owner may self-demote only when another active owner exists.
Only an owner may grant `OWNER`, demote an owner, or suspend/revoke an owner.

Every owner-sensitive role or status operation locks the Workspace row as the
canonical serialization point and rechecks authorization after acquiring it.
The transaction then verifies that at least one active owner remains. Two
concurrent owner demotions/revocations cannot both succeed.

Suspension and revocation set `active_workspace_id = NULL` on every customer
session for the affected user and Workspace. Sessions for other Workspaces are
untouched. Role capability checks load membership/role state on every action,
so role changes apply on the next request without relogin.

## Workspace settings

`/app/settings` shows Workspace name and status, current user and role, product
entitlements, and Usage. Users with `workspace.settings.manage` may rename an
active Workspace. Names are whitespace-normalized, required, and limited to
250 characters. Workspace status is read-only, and 03D has no close/delete
action.

The rename service rejects the same `Cc`, `Cf`, and `Cs` categories on the raw
name before whitespace collapse. Invalid characters return
`workspace_name_invalid_character` (400); empty and overlength names keep
their separate existing errors. Failed validation leaves the name and success
audit unchanged.

Entitlements and their limits are product/billing authority. Customer HTML and
API surfaces expose them read-only and cannot enable paid features or change
limits.

## Usage semantics

`WorkspaceUsageView` is the shared projection for HTML and API. Its queries are
tenant scoped and aggregate counts without per-row lookups. It reports:

- active members, unexpired pending invitations, member feature state and seat
  limit;
- saved-company usage, limit, and remaining capacity;
- monitoring feature state and active/paused subscription counts;
- stored report count and configured report limit;
- Bulk job count, Bulk feature state, and the unique-INN limit **per job**;
- Workspace status and creation date;
- all persisted entitlement keys, enabled flags, and limits.

Usage counts, feature-enabled state, and limits remain separate concepts. The
Bulk per-job maximum is never described as monthly consumption, and no billing
period is invented.

## Surfaces and audit

HTML mutations and their authenticated API equivalents call the same service
functions and require CSRF. Tenant-owned identifiers are always resolved with
the active Workspace; foreign membership, invitation, and role identifiers
return safe denials without metadata.

Audited actions are:

- `workspace.member.invite`
- `workspace.member.invite_reissue`
- `workspace.member.invite_revoke`
- `workspace.member.accept`
- `workspace.member.role_change`
- `workspace.member.suspend`
- `workspace.member.reactivate`
- `workspace.member.revoke`
- `workspace.settings.update`

Audit targets contain invitation, membership, or Workspace-safe identifiers.
Passwords, password hashes, invitation tokens, and token hashes are excluded.

All Workspace responses, including invite pages, inherit the existing
`noindex`, no-cache, CSP, framing, and referrer security headers. No customer
email or administration data is added to public HTML/API/card projections.

## Known gaps retained intentionally

- external invitation email delivery;
- custom roles and editable permission matrices;
- billing, tariff purchase, or entitlement self-edit;
- general password reset/change flows;
- Leave Workspace UX;
- Workspace deletion or closure.

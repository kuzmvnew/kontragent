# Workspace Product Completion 03A

Task: `MAC-OFFLINE-WORKSPACE-PRODUCT-COMPLETION-03A-CORE-WORKSPACE-01`

## Implemented product flow

The server-rendered Workspace now supports the complete core path:

1. Login and select an active Workspace.
2. Open a real tenant-scoped dashboard.
3. Search the canonical company projection.
4. Open the authorized Company Card.
5. Save the global company into the active Workspace.
6. Filter Saved Companies by name or INN.
7. Create, update, or clear a normalized Workspace note.
8. Enable Monitoring through the existing Monitoring P0 lifecycle.
9. Review active and paused subscriptions on the Monitoring overview.
10. Filter the persisted event feed by all, unread, read, and severity.
11. Mark an event read using the canonical `WorkspaceFeedEntry.read_at` state.
12. Return to the Company Card, pause or resume Monitoring, and logout.

No frontend-owned company representation, provider payload, RAW object, or duplicate monitoring lifecycle was added.

## Application shell and routes

The authenticated shell exposes only implemented areas and marks the current area with `aria-current="page"`:

- `GET /app` — dashboard and quick search.
- `GET /app/search` — canonical Workspace company search.
- `GET /app/companies/{inn}` — authorized Company Card and product actions.
- `POST /app/companies/{inn}/save` — save.
- `POST /app/companies/{inn}/unsave` — safe unsave; active Monitoring blocks deletion.
- `GET /app/saved` — bounded Saved list and server-side `q` filter.
- `POST /app/saved/{saved_company_id}/note` — note update or clear.
- `GET /app/companies/{inn}/monitoring` — company Monitoring lifecycle page.
- `POST /app/companies/{inn}/monitoring/{enable|pause|resume}` — existing Monitoring P0 mutations.
- `GET /app/monitoring` — subscription overview and bounded event feed; supports `state` and `severity` filters.
- `POST /app/monitoring/feed/{entry_id}/read` — persisted read action.
- `POST /logout` — audited session logout.

All HTML mutations remain POST-only and use the session CSRF contract. Validated `return_to` values preserve the user's current Workspace destination after Monitoring and feed actions.

API parity is provided through the existing Workspace API plus:

- `GET /app/api/dashboard`
- `GET /app/api/saved-companies?q=...`
- `PATCH /app/api/saved-companies/{saved_company_id}/note`
- `GET /app/api/monitoring?state=...&severity=...`

HTML and API mutations call the same Saved and Monitoring services.

## Permissions and entitlements

No new broad permission was introduced.

- Dashboard shell and Saved activity require `workspace.view`; Monitoring metrics and events are assembled only when `monitoring.manage` is authorized.
- Company search and card access use `company.search` and `company.view`.
- Save and note mutations use `company.save` plus `saved_companies.enabled`.
- Unsave uses `company.unsave` plus `saved_companies.enabled`.
- Monitoring enable, pause, resume, persisted feed API access, and read mutation use `monitoring.manage` plus `monitoring.enabled`.
- The Monitoring HTML overview can render an honest empty/blocked state for an otherwise authorized owner/admin when Monitoring is not entitled. Monitoring data and mutation controls still require `monitoring.manage`.

Authorization is always enforced in services. UI visibility is not an authorization boundary.

## Dashboard contract

`workspace_app.dashboard_service.get_workspace_dashboard` returns one stable `WorkspaceDashboard` object with:

- canonical Saved quota usage (`enabled`, `used`, `limit`, `remaining`);
- Saved count, active subscription count, paused subscription count;
- unread and total persisted feed counts;
- at most ten recent Saved companies;
- at most ten recent Monitoring events with read state.

The Saved limit and remaining capacity come from the same `saved_companies.enabled` entitlement record used by the Save service. No dashboard value is hardcoded.

## Saved contract

- `Company.id` remains global; `SavedCompany.workspace_id` remains the tenant boundary.
- Saved queries are workspace-scoped, bounded, deterministic, and filter in PostgreSQL by canonical company name/short name/INN.
- Monitoring state is resolved by an outer join to `MonitoringSubscription`; it is not copied into `SavedCompany`.
- Notes are collapsed to normalized whitespace, limited to 2,000 characters, and blank input clears the value.
- Note lookup and locking use both `SavedCompany.id` and the active `workspace_id`.
- Successful/unchanged update and clear requests write `company.saved_note.update` or `company.saved_note.clear` audit events.
- Active Monitoring continues to block unsave with the accepted `monitoring_active` conflict.

## Monitoring overview contract

- Subscription rows contain canonical Company name/INN, `ACTIVE` or `PAUSED`, last check time, and latest delivered event time.
- Subscription and event queries are bounded and workspace-scoped.
- Latest event resolution uses one grouped tenant query, avoiding per-company lookups.
- Feed filtering is server-side for `all`, `unread`, `read`, and optional `INFO|LOW|MEDIUM|HIGH` severity.
- Dashboard, HTML Monitoring, and API Monitoring all read `WorkspaceFeedEntry.read_at`; no duplicate read-state storage exists.
- Pause/resume/enable continue to call the accepted Monitoring P0 service and preserve immutable baselines, privacy barriers, coverage recovery, and generic semantic change detection.

## Tenant and privacy guarantees

Tests cover cross-Workspace denial for Saved IDs, note updates, subscription IDs, pause/resume attempts, feed entry reads, and independent lists. Queries always include `workspace_id`; a globally valid object ID never grants tenant access.

Public Card rendering and the public API are unchanged. Authenticated notes, Saved state, subscriptions, feed rows, and Workspace identity are never added to public projections.

## Known remaining gaps

- Reports / Export
- Bulk Check
- Users / Roles UI
- Workspace Settings / Usage UI
- Demo Workspace

These are intentionally outside 03A. The shell contains no links to them.

## Operational boundary

This change does not run or modify HOME execution, C6 controlled live, source ingestion scheduling, production source jobs, production databases, or deployment state.

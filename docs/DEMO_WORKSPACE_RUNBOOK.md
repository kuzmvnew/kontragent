# NEXT Company Demo Workspace 1.0 — local runbook

This runbook starts a real local Public application and Workspace application
against two dedicated PostgreSQL databases. The cohort is synthetic. It is not
production source evidence and must never be used to check a real counterparty.

## Safety boundary

The commands refuse any database host except `localhost` or `127.0.0.1` and
require every database name to contain `demo`. There is no override flag. Demo
mode does not bypass authentication, sessions, CSRF, RBAC, entitlements, quotas,
or tenant scope. Public reads still use `PublicRepository` and the active public
release tables.

Use only disposable local databases named:

- `nextcompany_demo_operational`
- `nextcompany_demo_public`

## 1. Create the local Demo databases

With local PostgreSQL running:

```zsh
createdb nextcompany_demo_operational
createdb nextcompany_demo_public
```

If these databases already exist and contain anything other than this Demo,
stop. Do not point these variables at a production or shared database.

## 2. Configure URLs and migrate explicitly

The following passwordless URLs assume the usual trusted local PostgreSQL
setup on macOS. Add the local username/password if your PostgreSQL installation
requires them.

```zsh
export DATABASE_URL='postgresql+psycopg://localhost/nextcompany_demo_operational'
export PUBLIC_IMPORT_DATABASE_URL='postgresql+psycopg://localhost/nextcompany_demo_public'

uv run alembic upgrade head
uv run alembic -c public_alembic.ini upgrade head
```

The bootstrap never auto-migrates. It requires operational head
`b5d7f9a1c3e6` and public head `public_0002`.

Create a read-only Public web role after the public migration:

```zsh
psql nextcompany_demo_public
```

Run the following in `psql`; `\password` prompts without putting the password
in repository files or shell history:

```sql
CREATE ROLE nextcompany_demo_public_web LOGIN;
\password nextcompany_demo_public_web
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO nextcompany_demo_public_web;
GRANT SELECT ON public_releases, public_company_projections, public_publication_state
  TO nextcompany_demo_public_web;
```

Then configure the runtime origins and read credential:

```zsh
export PUBLIC_DATABASE_URL='postgresql://nextcompany_demo_public_web:YOUR_LOCAL_PASSWORD@127.0.0.1/nextcompany_demo_public'
export PUBLIC_ORIGIN='http://127.0.0.1:8080'
export WORKSPACE_ORIGIN='http://127.0.0.1:8081'
export NEXTCOMPANY_DEMO_MODE=1
export PUBLIC_FORCE_NOINDEX=1
export NEXTCOMPANY_DEMO_EXPECTED_SHA="$(git rev-parse HEAD)"
```

`NEXTCOMPANY_DEMO_EXPECTED_SHA` is an assertion, not an override. Bootstrap
always resolves the actual repository `HEAD`, requires a canonical 40-character
lowercase SHA, and fails if the expected value differs. The same exact SHA is
written to both source provenance fields in the Demo release manifest and to
`public_releases.source_main_sha`. A stable Demo release created from another
SHA requires an explicit Demo reset; it is never overwritten in place.

## 3. Set the Demo owner password

The password is never stored in source or printed by Demo tooling. It must meet
the real Workspace policy (at least 14 characters):

```zsh
read -s 'NEXTCOMPANY_DEMO_PASSWORD?Demo owner password: '
export NEXTCOMPANY_DEMO_PASSWORD
echo
```

Owner email: `demo.owner@nextcompany.local`.

## 4. Bootstrap CLEAN or SHOWCASE

CLEAN is the acceptance starting point: cohort, owner, Workspace and
entitlements, with no user-created product state.

```zsh
uv run python scripts/bootstrap_workspace_demo.py bootstrap --profile clean
```

SHOWCASE creates representative Saved, Monitoring event, Report, Bulk and
member state through the real production services:

```zsh
uv run python scripts/bootstrap_workspace_demo.py bootstrap --profile showcase
```

Repeated bootstrap is idempotent for an already-correct profile and reports
`already_ready`. To change profile or return to a known empty state, reset first.

## 5. Run the Demo stack

```zsh
uv run python scripts/run_demo_stack.py
```

Open:

- Public: <http://127.0.0.1:8080>
- Workspace: <http://127.0.0.1:8081>

Both applications run in the foreground. `Ctrl+C` terminates both Uvicorn
processes; the runner waits and kills a child only if normal termination times
out.

## 6. Login and demonstrate the product

1. Search Public for `9000000046` and open the synthetic company card.
2. Confirm the persistent Demo banner and four `Проверка ещё не выполнена`
   official-source states.
3. Choose **Открыть в кабинете** and log in as
   `demo.owner@nextcompany.local` with the environment password.
4. Visit every main navigation item: Главная, Поиск, Сохранённые, Мониторинг,
   Отчёты, Массовая проверка, Пользователи, Настройки.
5. Search an exact Demo INN, save the company, add a note, and enable monitoring.
6. Generate and download a Report as JSON and CSV.
7. Upload a Bulk CSV containing several Demo INNs, a duplicate, and an invalid
   value; process it and export the historical result.
8. Create an invitation, accept it in a clean browser context, change the role,
   and confirm owner-only actions are denied to a MEMBER.
9. Rename the Workspace and compare Settings Usage with the created state.
10. Log out and confirm `/app` requires login again.

## 7. Advance a real monitoring event

After saving `9000000046` and enabling its monitoring subscription:

```zsh
uv run python scripts/demo_workspace.py advance-event --inn 9000000046
```

The command changes one synthetic semantic fact, calls the real
`monitor_company_once` path, and persists a canonical `MonitoringEvent` plus
tenant-scoped `WorkspaceFeedEntry`. It exposes no HTTP Demo mutation endpoint.

Verify safe database truth without printing credentials or tokens:

```zsh
uv run python scripts/demo_workspace.py verify
```

## 8. Stop and reset

Stop the runner with `Ctrl+C`. Reset is destructive only inside recognized Demo
databases and requires the exact confirmation token:

```zsh
uv run python scripts/bootstrap_workspace_demo.py reset --confirm LOCAL_DEMO_ONLY
```

Reset completes a read-only ownership preflight across both databases before
the first mutation. Public releases must all use the `nextcompany-demo-`
namespace. Every cohort company must carry the complete `NEXTCOMPANY_DEMO` /
`DEMO_SYNTHETIC` provenance object. A Demo Workspace is recognized only when
all six required entitlements use `policy_version=workspace-demo-v1` and the
Demo owner has an active `OWNER` membership. Display-name, email and cohort-INN
matches alone never authorize deletion.

The preflight also refuses partial entitlement markers, a same-name unproven
tenant, Demo owner/member memberships outside the proven tenant, or a Demo user
session scoped to another Workspace. On refusal, both databases remain
unchanged. After a successful preflight, reset deletes only the exact immutable
Workspace, user, company and public-release IDs in the validated plan.

Operational and public databases cannot share one atomic PostgreSQL commit.
Reset therefore validates everything first, commits the operational exact-ID
transaction, then clears the already-validated public release IDs. If the
operational transaction fails, public evidence is untouched; the public phase
is retryable if its later commit fails.

## What this proves

This Demo proves a local functional journey through FastAPI, PostgreSQL,
sessions, CSRF, RBAC, entitlements, Saved, Monitoring, Reports, Bulk,
Users/Roles, Settings/Usage, the release importer and `PublicRepository`.

It does **not** prove production source ingestion, production data acceptance,
public release acceptance, SEO readiness, deployment, source credentials,
background scheduling, real invitation email delivery, or correctness for any
real company.

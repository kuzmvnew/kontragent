# LOCAL REAL DATA PREVIEW — ООО «АЛАН»

This runbook opens the current Public Card and Workspace for retained real data
for ИНН `0100000614`. It is deliberately local, non-production, no-index, and
does not run HOME, ingestion, crawlers, workers, or monitoring checks.

## Fixed inputs and safety boundary

- Code revision: `c9bdb49768dc503af8ec82bdaa97653bc7412f3b` plus this task's reviewed changes.
- Read-only retained source: `public_card_binding_impl_01_20260928`.
- Disposable operational clone: `alan_preview_operational_20261009`.
- Disposable public projection DB: `alan_preview_public_20261009`.
- Company: ООО "АЛАН", ИНН `0100000614`.
- Public release: `local-real-preview-alan-v1`, exactly one company, never index eligible.
- Owner: `alan.preview.owner@nextcompany.local`.

The bootstrap rejects remote database hosts, unexpected database names, reused
production/release databases, demo mode, and a checkout that is not based on the
fixed canonical revision. The source connection uses `SET TRANSACTION READ ONLY`.

## 1. Create disposable databases

Use the PostgreSQL 18 server already running on the local Mac socket. These
commands create new databases; they do not change the retained source.

```sh
/opt/homebrew/opt/postgresql@18/bin/createdb -h /tmp \
  -T public_card_binding_impl_01_20260928 \
  alan_preview_operational_20261009
/opt/homebrew/opt/postgresql@18/bin/createdb -h /tmp \
  alan_preview_public_20261009
```

Set local URLs. Choose a fresh password of at least 16 characters; it is only
used for the disposable Workspace owner.

```sh
export NEXTCOMPANY_LOCAL_REAL_PREVIEW=1
export NEXTCOMPANY_DEMO_MODE=0
export ALAN_PREVIEW_SOURCE_DATABASE_URL='postgresql+psycopg://localhost/public_card_binding_impl_01_20260928?host=/tmp'
export DATABASE_URL='postgresql+psycopg://localhost/alan_preview_operational_20261009?host=/tmp'
export PUBLIC_IMPORT_DATABASE_URL='postgresql+psycopg://localhost/alan_preview_public_20261009?host=/tmp'
export PUBLIC_DATABASE_URL="$PUBLIC_IMPORT_DATABASE_URL"
export NEXTCOMPANY_PREVIEW_PASSWORD='replace-with-a-local-password'
```

## 2. Migrate only the disposable databases

```sh
uv run alembic upgrade head
uv run alembic -c public_alembic.ini upgrade head
```

Expected operational head: `b5d7f9a1c3e6`. Expected public head:
`public_0002`.

## 3. Export, import, and verify

```sh
uv run python scripts/bootstrap_local_real_preview.py prepare
uv run python scripts/bootstrap_local_real_preview.py verify
```

The first command reads the retained source, validates the physical Firmoteka
files/checksum and matching persisted Risk v3/Summary v3, exports one strict
`PublicProjection`, activates it only in the disposable public DB, and creates
the local owner only in the disposable operational clone. The evidence file is:

`public_releases/local-real-preview-alan/local-real-preview-alan-v1/local-preview-evidence.json`

The release directory is git-ignored and is safe to recreate. `prepare` is
idempotent only for the same exact release and workspace. It refuses unrelated
content instead of overwriting it.

## 4. Start both applications

```sh
uv run python scripts/run_local_real_preview.py
```

- Public Card: <http://127.0.0.1:8080/companies/0100000614>
- Workspace: <http://127.0.0.1:8081/login?return_to=%2Fapp%2Fcompanies%2F0100000614>

Log in with `alan.preview.owner@nextcompany.local` and the value of
`NEXTCOMPANY_PREVIEW_PASSWORD`. The Public Card and Authorized Card expose the
same `data-view-revision` from the imported projection.

## 5. Intended manual path

1. Open the Public Card and confirm the LOCAL/NON-PRODUCTION banner, real name,
   INN, requisites, facts, finances, source dates, Risk, Summary, and provenance.
2. Select **Открыть в кабинете**, log in, and search for `0100000614` if needed.
3. Confirm the Authorized Card has the same Company View revision.
4. Save the company.
5. Enable Monitoring. This creates only a zero-event baseline. No worker or
   source check is run, and the UI explicitly says that an empty feed is not a
   clean result.
6. Generate and download a report. It snapshots the same accepted
   `PublicProjection`; Risk/Summary are not recalculated in the UI.

## 6. Targeted acceptance

After `prepare`, run:

```sh
uv run python -m pytest tests/test_local_real_preview.py -q
LOCAL_REAL_PREVIEW_E2E=1 uv run python -m pytest \
  tests/test_local_real_preview_alan_e2e.py -q
uv run python -m pytest \
  tests/test_workspace_p0.py \
  tests/test_monitoring_p0.py \
  tests/test_workspace_reports.py \
  tests/test_public_web.py \
  tests/test_public_projection.py -q
```

The browser E2E uses the prepared disposable databases and performs Public Card
→ Login → Search → Authorized Card → Save → Monitoring → Report. It does not
produce a monitoring change event.

## Limitations

- This is a retained source revision, not a live source refresh and not the
  pinned revision expected by `tests/test_company_view_real_e2e.py`.
- The persisted Risk result is partial and carries real limitations. Zero risk
  factors must not be interpreted as a clean result.
- Monitoring has no live execution in this preview. Only the initial snapshot
  can be recorded; no event is fabricated.
- No public release, deploy, production mutation, data-rights change, HOME run,
  ingestion, or mass crawl is part of this workflow.

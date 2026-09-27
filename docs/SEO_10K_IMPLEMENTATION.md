# SEO 10K implementation foundation

Task: `SEO-10K-IMPL-01-A`

Status: implemented and locally testable; **not production accepted**

Release status: Wave 500 / 2000 / 10000 **NOT RELEASED**

## Architecture

`public_app.seo` is a pure compiler over one immutable `PublicProjection` and a
release-owned `SeoEligibilityContext`. It produces one typed `SeoProjection`
containing metadata, JSON-LD, canonical/robots decisions, deterministic hashes,
sitemap/catalog membership and internal evidence. Routes receive only the
compiled render fields; reason codes and evidence references are not added to
ordinary HTML or API responses.

The active public release remains the ownership boundary. The importer compiles
SEO rows in the same database transaction as the existing projection import.
`public_0002` adds nullable versioned SEO columns and indexes to the isolated
public database; it does not touch operational PostgreSQL. New code detects the
schema and keeps a bounded v1 fallback for the current 40-card database, which
supports mixed-version rollout. The migration is committed but not deployed by
this task.

Sitemap and catalog reads are set-based when `public_0002` is present. The
catalog uses `LIMIT/OFFSET` with a fixed page size of 24. The 16 stable company
shards use the first hexadecimal digit of SHA-256(INN). Import reuses the prior
SEO `content_updated_at` when `search_visible_hash` is unchanged.

## Commands

Focused foundation suite:

```bash
PYTHONPATH=. .venv/bin/python -m pytest \
  tests/test_public_seo.py tests/test_public_web.py \
  tests/test_public_semantic.py tests/test_public_projection.py \
  tests/test_public_seo_migration.py tests/test_seo10k_acceptance_evidence.py -q
```

Machine-readable acceptance evidence:

```bash
PYTHONPATH=. .venv/bin/python scripts/accept_seo10k_foundation.py \
  --output docs/evidence/SEO-10K-IMPL-01-A.json
```

Public PostgreSQL upgrade/downgrade verification requires an isolated test
database and `PUBLIC_TEST_DATABASE_URL`; no migration command targets
production in this task.

## Explicitly not accepted here

- Live Wordstat and Google historical demand evidence.
- Wave 500, Wave 2000 or Wave 10000 activation.
- Search Console and Yandex Webmaster production observation.
- Production sitemap submission, cache purge, bot-peak/TTFB acceptance.
- Production migration/deployment and changes to the current 40-card cohort.

These remain `NOT_RUN` in the A01–A22 evidence matrix and are release-blocking.

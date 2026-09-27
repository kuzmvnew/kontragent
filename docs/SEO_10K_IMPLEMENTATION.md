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

The active public release is the serving boundary, but it is not SEO release
authorization. The importer compiles versioned SEO rows in the same database
transaction as a new projection import and records the derived-state compiler
version. `public_0002` also adds explicit `seo_release_cohort` and
`seo_released` fields to the isolated public release table. They default to no
authorization; the migration does not backfill legacy rows or assign INDEX.
The migration is committed but not deployed by this task.

The company HTML route reads release id, public facts and persisted SEO state
with one repository statement, then verifies that every revision id agrees.
Missing, malformed, unauthorized or revision-mismatched SEO state is rendered
as `noindex, follow`; it is never upgraded to INDEX at request time. Each API
request also reads one public projection revision. Separate HTML and API
requests may legitimately observe different releases after an atomic switch,
but no single HTML response mixes facts, metadata, OpenGraph or JSON-LD.

Sitemap and catalog reads are set-based and require an explicit released SEO
cohort plus a complete, coherent stored INDEX row. Legacy rows and partial
storage produce no membership; stored NOINDEX always wins over what a runtime
compiler might otherwise derive. The catalog uses `LIMIT/OFFSET` with a fixed
page size of 24. The 16 stable company shards use the first hexadecimal digit
of SHA-256(INN). Import reuses the prior SEO `content_updated_at` when
`search_visible_hash` is unchanged.

Repeated import distinguishes a legacy release by its null release-level SEO
contract marker. Such a release must still have all derived SEO fields null and
is accepted idempotently without mutation. A native-v2 release has a compiler
marker and must reproduce every stored derived field; partial or tampered state
fails.

## Commands

Focused foundation suite:

```bash
PYTHONPATH=. .venv/bin/python -m pytest \
  tests/test_public_seo.py tests/test_public_web.py \
  tests/test_public_semantic.py tests/test_public_projection.py \
  tests/test_public_seo_migration.py tests/test_seo10k_contract.py \
  tests/test_seo10k_acceptance_evidence.py -q
```

Machine-readable acceptance evidence:

```bash
PYTHONPATH=. .venv/bin/python scripts/accept_seo10k_foundation.py \
  --output docs/evidence/SEO-10K-IMPL-01-A.json
```

PostgreSQL atomic-switch, mixed-version, importer and tamper regressions require
an isolated test database and `PUBLIC_TEST_DATABASE_URL`; no migration command
targets production in this task.

The evidence generator runs each `SEOIMPL-A*` gate from its own explicit test
IDs. Skipped tests become `NOT_RUN`, never PASS. Canonical
`SEO10K-A01...A22` retain the names and meanings in the approved contract and
remain a separate release matrix.

Evidence is bound to an immutable source commit and tree. Because writing the
JSON changes Git HEAD, the final evidence-only commit must be a direct child of
the recorded `head_sha` and may change only
`docs/evidence/SEO-10K-IMPL-01-A.json`.

## Explicitly not accepted here

- Live Wordstat and Google historical demand evidence.
- Wave 500, Wave 2000 or Wave 10000 activation.
- Search Console and Yandex Webmaster production observation.
- Production sitemap submission, cache purge, bot-peak/TTFB acceptance.
- Production migration/deployment and changes to the current 40-card cohort.

The canonical A01–A22 release matrix remains `NOT_RUN` and release-blocking.

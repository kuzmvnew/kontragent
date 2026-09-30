# Production-parity staging

Task: `STAGING-PARITY-01`
Expected PostgreSQL: **18.6 exactly**
Status of this change: implementation/IaC only; staging is not deployed and
production is unchanged.

## Topology and boundary

Staging is one isolated Linux worker with the same application runtime,
migration trees and generated systemd behavior used by production:

| Concern | Staging contract |
|---|---|
| Runtime | Python 3.14.x embedded in one immutable release archive; exact patch version and runtime-tree SHA are recorded |
| Operational DB | PostgreSQL 18.6 database `nextcompany_staging_operational`; separate owner/credential |
| Public DB | PostgreSQL 18.6 database `nextcompany_staging_public`; separate importer and read-only web credentials |
| Services | Generated from every checked-in `deploy/systemd/*.service` and `*.timer`; only names, environment, identities, paths and ports differ |
| Release artifact | `nextcompany-<git-sha>.tar.gz` plus SHA-256 sidecar and manifest |
| Network | Applications and Nginx bind loopback only; remote access is by private VPN/SSH tunnel and Nginx basic auth; no public DNS |
| Backup path | `/var/backups/nextcompany-staging-operational` and `/var/backups/nextcompany-staging` |
| Evidence | `/var/lib/nextcompany-staging/evidence/<UTC run>/` |
| Logs | systemd journal for installed services; acceptance runtime logs beside the evidence JSON |
| Health | operational `/api/health`; public `/api/health` and `/api/ready` |
| Search indexing | `PUBLIC_FORCE_NOINDEX=1`, `robots.txt` disallows `/`, every response has `X-Robots-Tag: noindex, nofollow, nosnippet` |

The scheduler, public sync and incident controller units are rendered to prove
parity, but are not enabled during acceptance. This prevents mass ingestion and
external source activity. Enabling them requires a separate deployment decision.

The loopback-only Nginx boundary is
deploy/nginx/nextcompany-staging.conf. A public bind, unauthenticated proxy or
public DNS entry is outside this contract and must fail review.

The dedicated PostgreSQL cluster must set the custom server parameter
nextcompany.environment = staging in postgresql.conf. The acceptance runner
verifies this server-side marker before creating any lane database; an admin
DSN pointing at an unmarked or production cluster fails closed.

## One artifact, one dependency resolution

Build once on the Linux release builder:

```bash
deploy/scripts/build_runtime_artifact.sh "$PWD" /var/lib/nextcompany-staging/artifacts
```

The builder refuses a dirty Git tree, exports exactly `HEAD`, creates a
relocatable Python 3.14 environment with `uv sync --frozen --no-dev`, records
the Python version, `uv.lock` hash, runtime tree hash, source SHA, systemd
hashes and PostgreSQL 18.6 contract, then writes the archive SHA-256. Before
hashing, it removes generated Python bytecode (which can contain the temporary
build path and build time) and canonicalizes directories, executable files and
non-executable files to safe deterministic modes. The manifest separately
records the source Git SHA and the builder Git SHA, builder script SHA-256 and
builder contract version.

The Linux CI gate builds twice into different temporary/output directories,
once with umask `0022` and once with `0077`, and requires byte-identical
archives, manifests and runtime-tree hashes. A build is not reproducible merely
because its source tree matches an earlier accepted source.

For an independently approved rebuild of a historical source, run the current,
clean builder and pass the clean historical worktree through `--source`. The
manifest then says `build_kind=historical_reacceptance`: `source_git_sha`
identifies the historical application tree while `builder.git_sha` and
`builder.script_sha256` identify the corrected builder. A missing historical
artifact SHA is never reconstructed or claimed from source. Only after the new
artifact completes independent acceptance may release evidence mark the old,
unavailable artifact `SUPERSEDED_UNAVAILABLE`.

Acceptance extracts that archive and executes its embedded Python and Alembic.
It does not run `uv sync`, pip, apt or any dependency resolver. The accepted
artifact SHA is written to `accepted-artifact.json`. Production promotion must
copy and verify that exact archive and checksum; rebuilding from Git, resolving
dependencies again or creating a second “production” artifact is forbidden.
Both staging activation and later production promotion use
`deploy/scripts/install_runtime_artifact.sh ARTIFACT ACCEPTED_SHA256`. The
installer verifies the accepted hash and stages the embedded runtime without a
package-manager call. After the shared migration wrapper succeeds, the same
command with `--activate` atomically points `current` at the hash-named
release.
Both environments run operational/public migrations through
`deploy/scripts/run_release_migrations.sh RELEASE_DIR LANE`; this wrapper has
no staging branch and invokes the release's embedded Python, canonical Alembic
configuration and migration tree.

## Input contracts

The staging worker retains the last accepted artifact and two custom-format
PostgreSQL backups (operational/public) with checksums. Their paths and rollback
contract are recorded from
`deploy/staging/previous-release-manifest.example.json`.

The production-shape lane consumes a sanitized pair of custom-format backups
described by
`deploy/staging/production-shape-manifest.example.json`. The manifest must say:

- sanitization completed;
- sensitive content is absent;
- mass ingestion is disabled;
- minimum cardinalities for selected relationship-bearing tables;
- any allowed whole extra schema, such as `legacy_v3_archive`.
- bounded semantic/release commands from the built-in allowlist, or an explicit
  per-release waiver when no semantic backfill exists.

Allowed extras are schema-scoped. Extra or changed objects inside the
application `public` schema still fail. A reproducible synthetic dataset may be
used instead of a production-derived restore only when it produces the same
manifest and relationship/cardinality contract.

The normal workflow never asks the owner to download evidence, copy a database
to a Mac or run ad-hoc commands. Chat 19 prepares/retains these sanitized inputs
on the isolated worker after its active production task is finished.

## Canonical acceptance

On the isolated PostgreSQL 18.6 worker:

```bash
set -a
source /etc/nextcompany-staging/staging.env
set +a
deploy/scripts/run_staging_acceptance.sh \
  /var/lib/nextcompany-staging/artifacts/nextcompany-<sha>.tar.gz \
  /var/lib/nextcompany-staging/inputs/previous-release.json \
  /var/lib/nextcompany-staging/inputs/production-shape.json
```

The runner requires the literal
`STAGING_ACCEPTANCE_CONFIRM=STAGING-PARITY-01`, accepts only an administrative
DSN whose database is `postgres` or `template1`, and creates only random
`sp01_*` databases. Names of production databases are explicitly protected.
Temporary databases are removed after the run. No production host, DSN or
credential is discovered.

The command performs:

1. Verify PostgreSQL server, `pg_dump` and `pg_restore` are 18.6.
2. Verify the candidate archive checksum, runtime-tree hash, lock hash, Python
   version and canonical unit hashes.
3. Render staging units from the production units and detect drift.
4. Migrate empty operational and public databases from zero to their current
   heads with the artifact's canonical `alembic.ini` files.
5. Run the operational ORM completeness audit and fingerprint both schemas.
6. In transactions, deliberately remove one table, column, non-constraint
   index and constraint while leaving Alembic at HEAD. Every mutation must be
   rejected by fingerprint comparison.
7. Restore the previous accepted operational and public backups, then run the
   same candidate migration commands and compare required schema objects.
8. Restore both sanitized production-shape backups, migrate them, verify
   cardinality and fingerprints, run only allowlisted semantic/release checks,
   and run no ingestion.
9. Start the candidate operational/public runtime from the accepted archive,
   check health/readiness and prove global noindex behavior.
10. Dump both shaped databases, checksum them, restore them into new databases
    and require identical fingerprints.
11. Simulate candidate runtime removal, start the previous accepted runtime
    against the post-migration databases and verify the public release pointer.
12. If a migration explicitly cannot support the previous binary, require the
    manifest's forward-recovery rule, reactivate the accepted candidate and
    prove health. An undocumented downgrade/forward path fails closed.

The same `run_release_migrations.sh` supplies staging fresh,
previous-release, production-shape and production promotion lanes for both
migration trees. There is no staging migration fork.

## Schema fingerprint

The deterministic payload includes:

- non-system schemas;
- tables, partitioned tables, views and materialized views;
- columns, formatted types, order, nullability, defaults, identity/generated
  attributes;
- indexes, uniqueness, predicates, validity and full definitions;
- primary/unique, foreign-key and check constraints;
- non-internal triggers;
- application functions/procedures with definition SHA-256;
- every Alembic revision.

Database name, OIDs, owners and timestamps are excluded so fresh, upgraded and
restored databases are comparable. A matching revision with a missing or
changed object is always a failure.

## Backup, rollback and forward recovery

Both database backups use PostgreSQL custom format, have SHA-256 sidecars and
are restored into different acceptance databases. The restored fingerprint
must exactly equal the source fingerprint.

Rollback first attempts the previous accepted runtime against the candidate
schema and checks operational/public health plus the unchanged public release
pointer. Each migration release must declare whether binary rollback is
supported. If it is not, the manifest must contain the exact forward-recovery
rule; acceptance then proves the candidate can be reactivated. Database
downgrade is never inferred from an Alembic `downgrade()` function.

## Evidence and gate

`acceptance-result.json` is the machine-readable release gate. A PASS contains:

- candidate artifact, source, runtime-tree and lock hashes;
- Python and PostgreSQL versions;
- operational/public revisions and fingerprints for every lane;
- false-HEAD mutation results;
- systemd parity and access-boundary status;
- runtime health/readiness and noindex proof;
- backup hashes and restored fingerprints;
- rollback mode, previous artifact hash and public release pointer.

Large dumps remain on the staging worker. Only compact JSON, hashes and logs are
needed for normal handoff. Production promotion is allowed only when every
mandatory gate is `PASS` and the promoted archive SHA equals
`accepted-artifact.json`.

## CI versus physical acceptance

`.github/workflows/staging-parity.yml` uses PostgreSQL 18.6 to exercise fresh
operational/public migration, deterministic fingerprints and all four
false-HEAD mutations, plus the pure contracts and the Linux byte-identical
double build. The full previous-release, sanitized-shape, service,
backup/restore and rollback gate runs on the isolated staging worker using the
single command above. This PR does not deploy it.

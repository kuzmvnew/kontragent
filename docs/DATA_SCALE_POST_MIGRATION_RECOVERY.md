# DATA-SCALE-03 post-migration recovery

This package repairs the migrated factory state without executing source
handlers, publishing cards, enabling schedules, or changing source activation
metadata. Runtime applicability remains exclusively authoritative from the
persisted `data_sets.applicability` column.

The operator entry point is:

```bash
PYTHONPATH=. .venv/bin/python scripts/recover_post_migration_factory.py
```

`--plan` and `--verify` start a read-only database transaction. `--apply` is
refused unless both the database revision and the exact running Git SHA are
confirmed. Apply commits applicability once, stale-worker recovery once, and
then uses stable, restart-safe batches of 100 by default.

The provisioning source of truth is
`app/services/dataset_applicability_policy.py`. It contains explicit policies
for all 45 dataset codes observed in the 2026-09-28 production diagnostic plus
the two already-existing future NRS registration paths. It is used only by
registration and recovery provisioning. Runtime decisions still read only the
persisted `data_sets.applicability` value; there is no code-based fallback.

The applicability mutation locks the small dataset catalog in one transaction
and changes only `data_sets.applicability`. It does not call the broad source
registry synchronizer. Subsequent state-repair batches may change only:

- canonical stale Worker lifecycle fields through `recover_stale_runs()` and
  its normal timeout/retry fencing path;
- a latest `company_enrichment_runs` lifecycle state, current Risk/Summary
  pointers, completion timestamp, and recovery error fields;
- `company_source_coverage` rows used to record exact-UUID successful
  `NOT_APPLICABLE` reconciliation, including the frozen policy/provenance;
- actionable `master_replay_signals` from `pending`/`scheduled` to `complete`
  only when their exact UUID is represented in that successful terminal
  coverage.

Failed replay rows remain `failed` and auditable. They block every readiness,
publication, factory metric, and ruleset-selection read path unless their exact
UUID is already present in successful terminal coverage for the same source
and current run. They are reported separately and never become actionable
backlog automatically. Unsafe current conclusions are parked fail-closed and
their run pointers are detached; immutable Risk and Summary rows are retained.

## Isolated restore rehearsal for Infrastructure / Parser Workers (19)

Run this only on the production host against the verified backup and a
disposable database. Do not point `RECOVERY_DATABASE_URL` at the operational
database during rehearsal.

```bash
set -euo pipefail

RECOVERY_ROOT=/home/mikhail/nextcompany-runtime/releases/ACCEPTED_RECOVERY_SHA
RECOVERY_BACKUP=/home/mikhail/nextcompany-backups/nextcompany_operational_20260928T061008Z.dump
RECOVERY_BACKUP_SHA=83c0c2d5c8749d6f38c9dd279e4490c8f27c275688ef8d070a22f1d42eff6fc7
RECOVERY_DB=nextcompany_operational_data_scale_recovery_rehearsal
RECOVERY_MAIN_SHA="$(git -C "$RECOVERY_ROOT" rev-parse HEAD)"
RECOVERY_DATABASE_URL="postgresql+psycopg://RECOVERY_USER:RECOVERY_PASSWORD@127.0.0.1:5432/$RECOVERY_DB"

test "$(sha256sum "$RECOVERY_BACKUP" | awk '{print $1}')" = "$RECOVERY_BACKUP_SHA"
dropdb --if-exists "$RECOVERY_DB"
createdb "$RECOVERY_DB"
pg_restore --exit-on-error --no-owner --no-privileges --dbname "$RECOVERY_DB" "$RECOVERY_BACKUP"

cd "$RECOVERY_ROOT"
export DATABASE_URL="$RECOVERY_DATABASE_URL"

PYTHONPATH=. .venv/bin/python scripts/recover_post_migration_factory.py \
  --plan --batch-size 100 \
  > data-scale-recovery-plan.json

PYTHONPATH=. .venv/bin/python scripts/recover_post_migration_factory.py \
  --apply \
  --expected-db-revision c2a4f6d8e0b1 \
  --expected-main-sha "$RECOVERY_MAIN_SHA" \
  --batch-size 100 \
  > data-scale-recovery-apply-1.json

PYTHONPATH=. .venv/bin/python scripts/recover_post_migration_factory.py \
  --verify \
  > data-scale-recovery-verify-1.json

PYTHONPATH=. .venv/bin/python scripts/recover_post_migration_factory.py \
  --apply \
  --expected-db-revision c2a4f6d8e0b1 \
  --expected-main-sha "$RECOVERY_MAIN_SHA" \
  --batch-size 100 \
  > data-scale-recovery-apply-2.json

PYTHONPATH=. .venv/bin/python scripts/recover_post_migration_factory.py \
  --verify \
  > data-scale-recovery-verify-2.json
```

Accept only when the first plan reports 45 datasets, zero unmapped policies,
the first verify reports all seven zero invariants, and the second apply reports
zero applicability mutations, zero replay coverage creation/terminalization,
zero recovered workers, and zero enrichment state changes.

`--verify` exits non-zero unless every persisted applicability policy is valid
and all seven required state invariants are zero. The exact invariants are:

- `public_ready_not_succeeded`
- `public_ready_not_complete`
- `public_ready_with_unresolved_replay`
- `public_ready_with_failed_replay`
- `active_enrichment_runs`
- `running_worker_jobs`
- `running_worker_runs`

```bash
jq '{applicability: .applicability.totals,
     invariants: .readiness_invariants,
     evidence: .evidence_counts}' data-scale-recovery-verify-1.json

jq '{applicability_mutations: .applicability.mutations,
     worker_recovered: .worker.recovered,
     replay: .not_applicable_replay,
     enrichment: .enrichment}' data-scale-recovery-apply-2.json
```

Compare `before.evidence_counts` and `after.evidence_counts` in the first apply.
Company, public-fact, registry-change, replay-signal, Risk, Summary, Firmoteka
crawl/raw/snapshot, and coverage counts must not decrease. Coverage may
increase only by the reported exact-UUID `NOT_APPLICABLE` reconciliation count.
Failed replay rows remain failed and auditable; successful terminal coverage
for their exact UUID makes them non-blocking without turning them into
actionable backlog.

The rehearsal is evidence only. It does not authorize a production apply,
runtime switch, service start, public publication, or 10K execution.

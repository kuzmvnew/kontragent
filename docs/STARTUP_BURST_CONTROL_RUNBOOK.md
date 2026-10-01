# Controlled worker startup

Task: `STARTUP-BURST-CONTROL-01`

The controlled-start command runs exactly one interval and exits. It registers
handlers, performs DB-only stale-run recovery, and may claim only existing jobs
whose source and lane are both explicitly admitted. It does not run
the due-schedule dispatcher or the automatic pre/post Master enrichment cycle.

The default work budget is 20 real work units. An ordinary job costs one unit;
jobs with explicit child collections cost their admitted child count.
Firmoteka `items` and `catalog_pages` are counted directly. A job whose cost is
greater than the remaining budget stays queued or retry-scheduled.
`controlled_work_units` may raise that observed cost but can never lower it,
and it cannot substitute for missing or malformed required batch metadata.

## Operator command

This is the production-shape one-shot entry point:

```bash
/opt/nextcompany/current/.venv/bin/python \
  -m scripts.run_data_readiness_scheduler \
  --controlled-start \
  --allow-source SOURCE_ID \
  --allow-lane source_control \
  --work-budget 20
```

Omit both `--allow-source` and `--allow-lane` to perform recovery and
observation without claiming any source work. Supplying only one is rejected.
Firmoteka is denied unless the
command contains both the literal `--allow-source firmoteka` and its explicit
lane admission (normally `--allow-lane master_intake`). Do not use `--loop`,
activation flags, or the source-worker service for controlled acceptance;
those combinations fail argument validation.

## Physical staging acceptance

Run this only after the merged release is installed on
`nextcompany-staging-01`. Do not run it from DEV. The acceptance intentionally
admits no source, so it cannot schedule, claim, or advance Firmoteka and cannot
make an external source request. Automated tests separately cover admitted
ordinary jobs and 10/40-child Firmoteka batches.

```bash
test "$(hostname -s)" = "nextcompany-staging-01"
! sudo systemctl is-active --quiet nextcompany-staging-source-worker.service

cd /opt/nextcompany-staging/current
set -a
source /etc/nextcompany-staging/staging.env
set +a

evidence_dir="/var/lib/nextcompany-staging/evidence/startup-burst-control-01"
install -d -m 0700 "$evidence_dir"

.venv/bin/python -m scripts.run_data_readiness_scheduler \
  --controlled-start \
  --work-budget 20 | tee "$evidence_dir/interval-1.json"

jq -e '
  .mode == "controlled_start" and
  .scheduler_admission == "off" and
  .scheduler_jobs_created == 0 and
  .work_units_admitted <= 20 and
  .firmoteka_jobs_created == 0 and
  .firmoteka_jobs_claimed == 0 and
  .firmoteka_work_units_admitted == 0 and
  .firmoteka_child_items_admitted == 0 and
  .automatic_enrichment == "off" and
  .enrichment_mutations == 0 and
  .raw_delta == 0 and
  .snapshot_delta == 0 and
  (.firmoteka_crawl_delta.count_delta == 0) and
  (.firmoteka_crawl_delta.created_ids == []) and
  (.firmoteka_crawl_delta.removed_ids == []) and
  (.firmoteka_crawl_delta.advanced_ids == [])
' "$evidence_dir/interval-1.json"

.venv/bin/python -m scripts.run_data_readiness_scheduler \
  --controlled-start \
  --work-budget 20 | tee "$evidence_dir/interval-2.json"

jq -e --slurp '
  .[0].jobs_after_by_status == .[1].jobs_before_by_status and
  .[1].scheduler_jobs_created == 0 and
  .[1].runs_created == 0 and
  .[1].work_units_admitted == 0 and
  .[1].work_units_remaining == 20 and
  .[1].firmoteka_jobs_created == 0 and
  .[1].firmoteka_jobs_claimed == 0 and
  .[1].firmoteka_work_units_admitted == 0 and
  .[1].automatic_enrichment == "off" and
  .[1].enrichment_mutations == 0 and
  .[1].raw_delta == 0 and
  .[1].snapshot_delta == 0 and
  (.[1].firmoteka_crawl_delta.advanced_ids == [])
' "$evidence_dir/interval-1.json" "$evidence_dir/interval-2.json"
```

The first interval may report stale run IDs and corresponding durable job
status changes. Therefore determinism is checked from interval 1's final state
to interval 2's initial state. Neither command starts or enables the continuous
scheduler/service.

## Explicit admitted rehearsal

After the default-deny acceptance, an operator may admit an already reviewed
non-Firmoteka queue source. Repeat `--allow-source` or `--allow-lane` to admit
multiple sources or lanes:

```bash
.venv/bin/python -m scripts.run_data_readiness_scheduler \
  --controlled-start \
  --allow-source REVIEWED_SOURCE_ID \
  --allow-lane source_control \
  --work-budget 20
```

The JSON report is the acceptance evidence: before/after job statuses, created
run IDs, claimed source counts, admitted/remaining work units, scheduler and
Firmoteka creation/claim counts, enrichment mutations, stale recovery IDs, RAW
and snapshot deltas, and Firmoteka crawl changes.

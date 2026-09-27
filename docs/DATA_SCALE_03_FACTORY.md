# DATA-SCALE-03 company factory

This document records measured production evidence and the fail-closed scaling
controls introduced by DATA-SCALE-03-FINAL. It is not evidence that the live
factory has passed the 10k/25k/50k/100k daily gates. Those gates require a
merged release, a verified pre-migration backup, deployment, and an observed
soak at each level.

## Live baseline

Read-only observations were taken on 2026-09-27 before this change was
deployed. Production was still at canonical SHA
`32334077b8f6a0ab77219aeee1aaed75c3389ae9`.

| Measure | Observed value |
| --- | ---: |
| Master | 2,108 |
| Fully enriched | 2,008 |
| Public ready | 2,098 |
| Active enrichment runs | 100 |
| Complete but not public ready | 10 IP records |
| MasterReplaySignal pending/scheduled, raw | 28,853 |
| Firmoteka unique fetched | 2,008 |
| Firmoteka unique discovered | 532,346 |
| Firmoteka backlog | 530,338 |
| Catalog pages | 2,848 / 17,401 |
| First-time fully enriched, trailing 24h | 2,108 |
| First-time public ready, trailing 24h | 2,098 |
| First-time fully enriched/public ready, trailing 1h | 0 / 0 |
| Firmoteka fetched, trailing 24h | 943 |
| Firmoteka fetched, trailing 1h | 0 |
| Queue latency, trailing 1h | p50 0.267s; p95 74.321s |
| PostgreSQL size | 1,379,481,267 bytes |
| Known RAW | approximately 2.742 GB |
| Disk | 975,252,967,424 bytes free of 1,081,101,176,832 |

The 2,100 Risk and Summary rows created in the sampled hour were repeated
recalculation, not company throughput. The persisted-evidence timestamp fix in
this change makes identical missing-source inputs reusable instead of creating
a new Risk/Summary generation on every scheduler poll.

The former apparent actionable replay backlog of 1,958 was caused by IP-only
`fns_npd` signals attached to ten-digit legal-entity INNs. The shared
applicability predicate now removes cross-scope signals from replay,
backpressure, and public-cohort recalculation. Raw signal count remains visible
for diagnosis; only actionable signals govern capacity.

The current sustained output is therefore reported conservatively as zero new
fully enriched/public-ready companies in the trailing observed hour, with
2,098 first-time public-ready companies in the trailing 24 hours. The worker
was caught in false backpressure, so configured capacity must not be presented
as measured throughput.

## Ranked bottlenecks

1. False global backpressure from non-applicable replay signals stopped
   Firmoteka intake while no real legal-entity work was actionable.
2. Risk and Summary missing-source evidence used calculation time as source
   observation time, producing continuous duplicate generations.
3. A single serial job loop allowed slow point sources to delay bulk and
   control work.
4. Bulk snapshot projection was not restricted to the requested enrichment
   cohort and could replay unrelated Master rows.
5. New operational sources had no restart-safe, source-only backfill generation
   for the existing Master.
6. Fixed intake thresholds did not include measured enrichment/Firmoteka rates,
   backlog age, database connections, CPU, disk, or RAW growth.

## Factory semantics

A processed company has a Master identity, every applicable current
operational source expectation resolved, a persisted Risk assessment and
Summary, and a current `public_ready` projection. A downloaded Firmoteka page
alone is not a processed company.

Source applicability is shared by scheduling, replay, generation, and pressure
metrics. Exact INN identity remains mandatory. Unavailable sources do not
become `NOT_FOUND`, and immutable provenance is retained.

An unresolved applicability decision is persisted as a
`company_source_coverage` row with semantic status `APPLICABILITY_UNKNOWN` and
execution status `blocked`. The row retains company, dataset, source, replay
signal IDs, the policy snapshot, and its frozen timestamp. It contributes to
the run's frozen `source_count`, creates no worker job, and is reported
separately from actionable backlog. Risk, Summary, fully-enriched throughput,
`public_ready`, and publication remain closed until every such row resolves.
Resolution to `NOT_APPLICABLE` records an explicit terminal decision;
resolution to `APPLICABLE` first creates normal source work.

The final replay scan and registry/Firmoteka replay emission serialize on the
same company row lock. Under PostgreSQL `READ COMMITTED`, a replay signal whose
writer commits before the Risk transaction acquires that lock must be included
in the current denominator. A writer acquiring the lock afterwards is ordered
after that projection and is handled by the next replay run. This transaction
boundary prevents a signal committed "just before Risk" from being skipped.

The scheduler exposes independent logical budgets for `master_intake`,
`bulk_enrichment`, `point_enrichment`, and `source_control`. Risk, Summary,
semantic projection, readiness, and publication remain explicit downstream
states; they are not calculated on page views. Point-source delay cannot consume
the bulk lane.

## Default controls

All limits are non-secret environment configuration and default to the last
proven production envelope:

| Control | Default |
| --- | ---: |
| Active enrichment runs | 100 |
| Refill low watermark | 50 |
| Bulk exact-join batch | 100 |
| Generation selection batch | 100 |
| Reconciliation batch | 100 |
| Master-intake jobs/cycle | 2 |
| Bulk jobs/cycle | 12 |
| Point jobs/cycle | 2 |
| Source-control jobs/cycle | 4 |
| Hard actionable backlog | 2,000 companies |
| Maximum backlog age | 3,600 seconds |
| Minimum disk free | 10% |
| Minimum disk runway at measured RAW growth | 72 hours |
| Maximum normalized host CPU | 90% |
| Maximum PostgreSQL connections | 80% |

`FactoryScaleConfig.from_environment()` reads the corresponding
`FACTORY_*` variables. Invalid or unsafe values fail during configuration
construction. These defaults are deploy-safe controls, not a claim of
100k/day throughput.

Adaptive intake is open when caught up and resources are safe. It throttles to
the measured enrichment/Firmoteka throughput ratio and pauses for hard backlog,
old backlog, disk, CPU, or database-connection pressure. The decision and its
inputs are persisted in the Firmoteka crawl cursor.

## Batch benchmark

The production benchmark is SELECT-only and uses exact identity joins against
accepted local snapshots. The live Master contained only 2,108 companies, so
the requested 5,000 live batch was correctly reported as a 2,108-row sample.

| Requested | Live sample | Target rows | Matched facts | Client wall | DB execute | Records/s |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 100 | 100 | 100 | 372 | 6.543ms | 1.164ms | 15,282 |
| 500 | 500 | 500 | 1,590 | 2.858ms | 2.174ms | 174,974 |
| 1,000 | 1,000 | 1,000 | 3,395 | 4.011ms | 3.437ms | 249,327 |
| 5,000 | 2,108 | 2,108 | 7,622 | 6.347ms | 6.361ms | 332,108 |

Client CPU was 0.555–1.398ms for the 500–2,108 live batches. Maximum RSS delta
was 512 KiB, WAL delta was zero, and failure recovery after a forced statement
timeout passed. Read-lock count reached 59 and remained stable. The selected
initial production batch remains 100 until the controlled ladder proves a
larger batch under concurrent write load.

The exact 5,000 shape was also exercised against an isolated PostgreSQL database
with 5,100 transaction-local identities and a full rollback: 5,000 target rows,
10.215ms client wall time, 3.525ms client CPU, 0.610ms planning, 5.893ms database
execution, 489,496 records/s, 640 KiB maximum RSS delta, zero WAL from the
SELECT-only join, 38→78 read locks, and forced-timeout recovery PASS. The
fixture had no fact rows, so this proves bounded join mechanics and recovery,
while the 2,108-row live result remains the representative fact-density sample.

Use `scripts/benchmark_company_factory.py --verify-failure-recovery` for a
read-only repetition. A live 5,000-row benchmark under concurrent writes is
still required before that batch size can be selected in production.

## Source and ruleset generations

When a dataset first becomes operational, a durable source generation selects
applicable existing companies in bounded ID order. Each membership is unique
for the generation and company. It schedules an enrichment run containing only
that source, then reuses the normal persisted-facts path for affected Risk,
Summary, and public readiness. Selection cursor, counts, status, and errors
survive restart. Existing operational datasets are adopted as complete during
migration so deployment cannot accidentally backfill every current source.

A versioned Risk or semantic ruleset generation selects completed companies and
recalculates from persisted evidence without source redownload. Re-registering
the same version is idempotent. Failed generation members remain inspectable and
can be retried within the normal restart bound.

Dataset metadata now carries `source_family`, `capability`, `risk_role`,
`positive_role`, `coverage_role`, `applicability`, `freshness`, `precedence`,
`publicability`, and `impact_priority`. This is an internal v4-compatible
foundation; it does not activate a public numeric score.

## Storage forecast

The forecast includes RAW, normalized/PostgreSQL heap and indexes, WAL, and two
backup generations. It is derived from the measured 2,108-company production
basis and should be recalculated after every scale gate.

| Master | RAW + fixed | PostgreSQL | WAL | Two backups | Total |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 10k | 4.01 GB | 1.77 GB | 0.46 GB | 11.56 GB | 17.79 GB |
| 100k | 17.68 GB | 9.96 GB | 4.55 GB | 55.28 GB | 87.47 GB |
| 1M | 154.34 GB | 91.90 GB | 45.52 GB | 492.46 GB | 784.21 GB |
| 12M | 1.82 TB | 1.09 TB | 546.22 GB | 5.84 TB | 9.30 TB |

The current approximately 1 TB volume does not provide the required 10% safety
margin at 1M and cannot support 12M. Before 1M, immutable RAW and backup
generations must move to separately sized object storage, PostgreSQL must use a
dedicated volume with growth alarms, and WAL archival/retention must have its
own budget. Provenance must not be deleted to reclaim space.

## Controlled ladder and gates

The only allowed live order is baseline, 10k/day, 25k/day, 50k/day, then
100k/day. Before migration or scale change: create the canonical backup, verify
its SHA256, and pass the isolated restore rehearsal. For every gate, observe at
least one representative operating window and require:

- no raw public object or internal-code leakage;
- stable applicable-source completeness and exact identity matching;
- no contradictory or unsupported Summary/recommendation;
- intact visible-text-to-provenance traceability;
- queue p95, retry rate, locks, WAL, database connections, CPU, memory, disk,
  and RAW growth within the configured bounds;
- representative cards covering current, `NOT_FOUND`, stale/historical,
  unavailable, positive, negative, limitation, no-factor, and multi-factor
  states;
- an explicit rollback point and no expansion when any gate fails.

The public cohort is separately controlled at 40, 500, 2,000, and 10,000.
Factory readiness never auto-publishes the entire Master or bypasses release,
SEO, Product, Legal, or QA acceptance. `PUBLIC_NEXT_INDEX_ENABLED` remains
false.

## Operator commands

Read-only measurement:

```bash
PYTHONPATH=. .venv/bin/python scripts/measure_factory_baseline.py \
  --window-hours 1 --sample-seconds 60
```

Batch benchmark:

```bash
PYTHONPATH=. .venv/bin/python scripts/benchmark_company_factory.py \
  --verify-failure-recovery
```

Storage forecast:

```bash
PYTHONPATH=. .venv/bin/python scripts/forecast_factory_storage.py
```

The admin source console shows compact factory totals, rates, backlog age,
source/worker queues, and RAW growth. Services remain autonomous systemd units;
no Codex, ChatGPT, Mac, or manual command is part of the steady-state loop.

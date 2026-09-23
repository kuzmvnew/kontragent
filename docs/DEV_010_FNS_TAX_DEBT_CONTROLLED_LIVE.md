# DEV-010 — S02 FNS Tax Debt controlled live pilot enablement

Status: implemented for QA. The pilot is disabled by default and no production
activation or mass ingestion is included.

## Official discovery

The only discovery entrypoint is the official FNS dataset page for
`7707329152-debtam`:

`https://www.nalog.gov.ru/opendata/7707329152-debtam/`

Discovery parses the current artifact URL, the explicitly published XSD URL,
the last-change date (`source_as_of`), the data date, and the official
actual-until date. Artifact and XSD URLs must use HTTPS, the exact
`file.nalog.ru/opendata/7707329152-debtam/` path, supported filenames, and the
same structure version. A missing XSD link is an error; the adapter never
constructs one and never crawls a directory.

## Handler and pilot isolation

The controlled handler is pinned to
`fns-tax-debt-controlled-live-v1`. It can be registered only after the durable
registry record is created by the explicit approval function. The job also
requires all of the following:

- `enabled=True` supplied deliberately (the code default remains false);
- pilot marker `s02-controlled-live-pilot`;
- 1–100 valid 10-digit legal-entity INNs in the cohort allowlist;
- an exact current official discovery result and non-stale actual-until date;
- artifact and XSD checksums calculated before enqueue.

The parser retains only cohort records after validating every XML member. The
publisher rechecks every staged INN against the allowlist and fails the whole
transaction if any record is outside it. Company lookup is exact-INN and
read-only; missing companies remain unmatched and are never created.

`MASS_INGESTION_ENABLED` remains `False`. There is no scheduler, production
activation, public route, API, Risk change, Summary change, or Public
Projection connection.

## XSD validation

The discovered XSD is stored at
`<artifact-store>/S02/xsd/<sha256>/structure.xsd` with an immutable manifest.
The handler verifies its expected checksum and validates every XML member with
network/entity resolution disabled. Schema load or validation failure stops
the run before normalization/publication.

## Generation rollback

Controlled publications receive immutable fact/query generations. Canonical
tax-debt rows carry their publication generation, and the existing query
service selects only the active query generation. The S02 rollback operation
atomically restores:

- active normalized and RAW pointers/checksum;
- dataset `source_as_of`, `retrieved_at`, data date, counts and coverage;
- the prior fact and query generation;
- validation metadata, counters and generation status.

The replaced generation remains the immediately recoverable rollback target.

## Monitoring

`get_fns_tax_debt_pilot_monitoring` exposes last discovery, last success,
active RAW pointer/checksum, pointer/fact/query generations, counters,
freshness, and recent discovery/worker errors. Freshness is recalculated from
the official actual-until date. Alert delivery intentionally remains an
adapter boundary.

## QA command

```text
PYTHONPATH=. .venv/bin/python -m pytest \
  tests/test_fns_tax_debt_pipeline.py \
  tests/test_fns_tax_debt_controlled_live.py -q
```

Coverage includes valid discovery, artifact change/no-change, no URL guessing,
default/over-100 cohort gates, outside-cohort fail-closed behavior, pinned XSD
failure, stale handling, same-artifact idempotency, no company creation, and
B→A generation rollback.

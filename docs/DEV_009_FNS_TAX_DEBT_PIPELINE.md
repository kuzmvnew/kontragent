# DEV-009 — S02 FNS Tax Debt vertical pipeline

Status: coding and fixture execution implemented. Controlled live pilot remains
gated pending QA. Mass ingestion is disabled.

## Source contract

| Field | Contract |
|---|---|
| `source_id` | `S02` |
| Dataset | `fns_tax_debt` / FNS dataset `7707329152-debtam` |
| Official source | `https://www.nalog.gov.ru/opendata/7707329152-debtam/` |
| Access | Official bulk ZIP snapshot; retain RAW before parsing |
| Format | ZIP with XML, `ВерсФорм=4.01`, `ТипИнф=ОТКРДАННЫЕ6` |
| Identifier | Valid 10-digit legal-entity INN; exact match only |
| Freshness | Dated source snapshot; `source_as_of` and `retrieved_at` are mandatory |
| Canonical fact | `tax.debt.amount_as_of_date` |
| Versions | `fns-debtam-xml-v1`, `tax-debt-normalization-v1` |

The executable contract is
`app/sources/fns_tax_debt.py`. The source does not represent a real-time tax
balance and does not prove referral to bailiffs.

## Vertical path

1. `enqueue_fns_tax_debt_fixture_job` creates an idempotent `S02` job using
   the RAW checksum, source date, and normalization version.
2. DEV-008 claims the job, obtains the PostgreSQL source lease and fencing
   token, starts a run, supervises timeout, retry, heartbeat, and counters.
3. The isolated handler copies the ZIP once into a checksum-addressed store,
   verifies SHA-256, and writes an immutable JSON manifest.
4. The handler validates the ZIP/XML schema, quarantines invalid documents,
   deterministically normalizes valid documents, deduplicates exact repeats,
   and quarantines contradictory `(INN, data_date)` records.
5. A replayable normalized staging file is written beside RAW. The handler
   returns only RAW/staging references and counters to the parent.
6. The optional transactional worker publisher resolves exact INN matches
   without creating companies, persists normalized/unmatched/conflict states,
   and projects matched records into `company_tax_debt_snapshots` as canonical
   facts with provenance and limitation states.
7. Worker RAW manifest, normalized rows, fact rows, dataset state, and active
   publication pointer commit in the same parent transaction. A publisher
   failure rolls all DB writes back and goes through the DEV-008 retry policy.
8. The existing `tax_debt_check` contract remains the only Risk/Summary input.
   No risk rules or Summary wording were changed.

## RAW and replay

The artifact directory is `<artifact-store>/S02/<sha256>/` and contains:

- `artifact.zip` — immutable source bytes;
- `manifest.json` — checksum, source/retrieval dates, source and versions;
- `normalized.json` — deterministic parser output and quarantine payload.

Re-running the same job reuses byte-identical files. Any attempt to change a
member under the same content address fails closed. The normalized staging
pointer can be replayed by the transactional publisher without fetching the
source again.

## Matching and facts

- one exact legal-entity INN match: `matched` / `inn_exact` and a fact;
- no company: `unmatched`, retained, no company and no fact are created;
- multiple candidates or incompatible entity type: `conflict`, no fact;
- invalid INN: quarantine;
- identical duplicate: counted and collapsed;
- contradictory duplicate identity/date: all conflicting records quarantined.

Canonical fact provenance includes the S02 id, official source, RAW reference
and SHA-256, worker run, XML member, source document, record hash, source and
retrieval dates, parser/normalization versions, and matching method.

## Public projection preparation

`prepare_tax_debt_public_projection` exposes only the input required by a
future Public Projection: result/applicability, amount, amount date, source,
source reference, provenance, limitations, and retrieval time. DEV-009 does
not connect this object to public routes or templates.

## Fixture command

```text
PYTHONPATH=. .venv/bin/python scripts/run_fns_tax_debt_fixture_worker.py \
  /path/to/official-format.zip \
  --artifact-store /path/to/raw-store \
  --source-as-of 2026-08-25T12:00:00+00:00 \
  --expected-sha256 <sha256>
```

This command is fixture-only. `controlled_live` is rejected in code until a
post-QA change explicitly opens the pilot gate. There is no downloader or mass
schedule in DEV-009.

## QA focus

- migrate an isolated PostgreSQL database to `d0e1f2a3b4c5`;
- run `tests/test_fns_tax_debt_pipeline.py` and worker/Risk/Summary regressions;
- verify upgrade, one-revision downgrade, and re-upgrade;
- inspect one fixture run, RAW checksum/manifest, quarantine, exact match,
  canonical provenance, counters, and publication pointer;
- confirm a simulated publisher outage leaves no partial domain rows and
  schedules retry;
- only after those checks, decide whether to enable a controlled live pilot.


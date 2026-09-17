# FNS Tax Offences Source Passport

Date: 2026-09-17
Stage: Intermediate Stage 1.5 / A2
Decision: **APPROVED_FREE_OFFICIAL**
Production mode: official annual bulk snapshot, manual ingestion, dated PostgreSQL history
Auto-update: **NOT_CONFIGURED**

## Source

- Owner/operator: Federal Tax Service of Russia (ФНС России).
- Official dataset id: `7707329152-taxoffence`.
- Official page: <https://www.nalog.gov.ru/opendata/7707329152-taxoffence/>.
- Official file base: <https://data.nalog.ru/opendata/7707329152-taxoffence/>.
- Dataset name: information about tax offences and measures of responsibility.
- Source class: `OPEN_DATA / OFFICIAL / BULK`.
- Machine format: ZIP containing XML files.
- Entity type: Russian legal entities with 10-digit INN.
- Matching: exact INN only. Name matching is not used.

## Accepted snapshot

- File: `data-20251201-structure-20191201.zip`.
- Official file publication: 2025-12-01; XML document date: `2025-12-02`.
- Source data period/date: `2024-12-31`.
- SHA-256: `1a388022e0db361dc1cc78d65b4eb6f5f08a1d1fc59a9c6d035e1e8b4b4e384b`.
- ZIP integrity: PASS; 111 XML files.
- Source documents: 100,005 valid; 0 invalid.
- Exact-INN Master matches/stored documents: 24,139.
- Unmatched source documents: 75,866; they are not fabricated in Master Registry.
- Matched companies: 24,139.

The official description limits the release to offences for which liability decisions entered into force in the previous calendar year and the fine remained unpaid by the stated publication cutoff. This is not a complete lifetime history of every tax offence.

## Facts and semantics

The XML publishes:

- exact legal-entity INN and source company name;
- source document identifier;
- document/publication date;
- data period/date;
- aggregate fine amount.

The current XML does not publish the exact offence type. The product must not invent or infer one.

Check states:

- `found`: an exact-INN record exists in the successfully loaded current snapshot;
- `not_found`: no exact-INN record exists in that successfully loaded dated snapshot;
- `not_applicable`: current dataset is for legal entities and is not applied to an IP;
- `unavailable`: company/dataset is missing or no successfully loaded dataset date exists.

Invalid money raises an error instead of becoming zero. A snapshot containing any structurally invalid document fails the import before `DataSet.last_data_date` is advanced. Source failure therefore cannot publish a new `not_found` coverage date.

## Access, cost and use decision

- Credentials/registration: none.
- CAPTCHA/challenge: none for the published bulk file.
- Access pattern: one official annual snapshot download, checksum, local validation, bulk import.
- Rate/load: no page crawling; download only the published file needed for an update.
- Source fee: 0.
- Storage: local official snapshot plus normalized facts and ingestion metadata.
- Cache/retention: retain checksum, dates, ingestion run and normalized dated records for reproducibility.
- Product display: attributed amount/count/date facts and explicit source limitations.
- Raw public export/API/republishing: not enabled by this workstream; reassess at the Legal Launch Gate.
- Personal data: not collected; this adapter accepts legal-entity INN only.
- Operational cost: approximately 12 MiB compressed / 36 MB uncompressed for the accepted release, PostgreSQL storage and manual operator time.
- Fallback: preserve the last successfully accepted snapshot and show `unavailable` if no valid snapshot is loaded. Do not substitute a commercial aggregator.

## Acceptance

Acceptance evidence: `docs/STAGE_1_5_A2_TAX_OFFENCE_ACCEPTANCE.md`.

# FNS Tax Debt Source Passport

Date: 2026-09-17
Stage: Intermediate Stage 1.5 / A1
Decision: **APPROVED_FREE_OFFICIAL**
Production mode: official bulk snapshot, manual ingestion, dated PostgreSQL history
Auto-update: **NOT_CONFIGURED**

DEV-009 integration update (2026-09-23): source `S02` now has a worker-backed
RAW/manifest/parser/normalization/exact-INN/fact pipeline for coding and local
official-format fixtures. The controlled live pilot remains gated until QA;
mass ingestion and automatic scheduling remain disabled. See
`docs/DEV_009_FNS_TAX_DEBT_PIPELINE.md`.

## Source

- Owner/operator: Federal Tax Service of Russia (ФНС России).
- Official dataset id: `7707329152-debtam`.
- Official page: <https://www.nalog.gov.ru/opendata/7707329152-debtam/>.
- Official file base: <https://file.nalog.ru/opendata/7707329152-debtam/>.
- Dataset name: information about tax, fee and insurance-contribution debt, penalties and fines in the Russian budget system.
- Source class: `OPEN_DATA / OFFICIAL / BULK`.
- Machine format: ZIP containing XML files; official XSD `structure-20181201.xsd`.
- Entity type: Russian legal entities with 10-digit INN.
- Matching: exact INN only. Name matching is not used.

## Accepted snapshot

- File: `data-20260825-structure-20181201.zip`.
- Official publication/document date: `2026-08-25`.
- Source data date: `2026-08-01`.
- SHA-256: `8f39aee65b09c716d0b3e5900ecf9efb8938a064b1f1eb491ea309385a8cda65`.
- ZIP integrity: PASS; 1,052 XML files.
- Imported documents: 946,887 valid; 0 invalid.
- Exact-INN Master matches: 625,395.
- Unmatched source documents: 321,492; they are not fabricated in Master Registry.
- Stored item rows: 2,031,361.

The official description says the snapshot reflects debt as of the stated source date and does not account for repayment after publication. Product wording therefore always retains the dataset date and does not present the value as a live balance.

## Facts and semantics

Stored facts:

- source document identifier and publication date;
- source data date;
- arrears;
- penalties;
- fines;
- total debt;
- itemisation by published tax/payment name.

Check states:

- `found`: an exact-INN record exists in the successfully loaded current snapshot;
- `not_found`: no exact-INN record exists in that successfully loaded dated snapshot;
- `not_applicable`: current dataset is for legal entities and is not applied to an IP;
- `unavailable`: company/dataset is missing or no successfully loaded dataset date exists.

An ingestion/parser error fails the load. Invalid money is never converted to zero, and a source total inconsistent with arrears + penalties + fines is rejected. A failed load does not advance `DataSet.last_data_date`, so it cannot become a dated `not_found` result.

## Access, cost and use decision

- Credentials/registration: none.
- CAPTCHA/challenge: none for the published bulk file.
- Access pattern: one official snapshot download, checksum, local validation, bulk import.
- Rate/load: no page crawling; download only the published file needed for an update.
- Source fee: 0.
- Storage: local official snapshot plus normalized facts and ingestion metadata.
- Cache/retention: keep checksum, dates, ingestion run and normalized dated snapshots for reproducibility.
- Product display: attributed derived facts with source/data dates.
- Raw public export/API/republishing: not enabled by this workstream; reassess at the Legal Launch Gate.
- Personal data: not collected; this adapter accepts legal-entity INN only.
- Operational cost: snapshot download, approximately 79 MiB compressed / 927 MB uncompressed for the accepted release, PostgreSQL storage and manual operator time.
- Fallback: preserve the last successfully accepted snapshot and show `unavailable` if no valid snapshot is loaded. Do not substitute a commercial aggregator.

## Separate FNS/bailiff-referral fact

This dataset is ordinary published tax-debt data. It does **not** prove that a debt above an applicable threshold was sent to a bailiff for enforcement.

The historically visible FNS/Transparent Business statement about threshold debt referred to a bailiff is a separate fact and must have its own official evidence, date and semantics. No current separate official bulk dataset was confirmed during A1 research, so that fact is not inferred from `debtam`, not joined to FSSP, and remains outside the accepted A1 product result.

## Acceptance

Acceptance evidence: `docs/STAGE_1_5_A1_TAX_DEBT_ACCEPTANCE.md`.

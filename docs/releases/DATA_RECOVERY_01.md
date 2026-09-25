# DATA-RECOVERY-01 — canonical public cohort forensic recovery

Date: 2026-09-25

## Decision

`public-v1-20260925T030601Z-ec8a39b4` is classified as
`SYNTHETIC_OR_DISPOSABLE_PUBLIC_TEST_COHORT` and marked
`SUPERSEDED_NOT_PRODUCTION_ELIGIBLE`.

The 40 INNs are real company identities, but the cohort is not a verified
operational cohort. Its exact set is the historical Product Recovery Golden-40
artifact from commit `66ec59d540feeff2733da1c4bbcdfd4e388b3be4`; PR #64 copied
that set into the public-release configuration. None of the eight restored
database backups contains any member of that public cohort.

The byte-exact old manifest is preserved at
`docs/releases/superseded/public-v1-20260925T030601Z-ec8a39b4-cohort.json`
with SHA256
`9b1f471be7789d6cc8a6387b600ba4cc3ce9623e6706b3293d6730afe5f9c0f8`.

## Backup forensics

| Backup | Alembic | Companies | Legal / IP | Old cohort | Exportable |
|---|---|---:|---:|---:|---:|
| `20260924T132951Z` | `f1a2b3c4d5e6` | 40 | 40 / 0 | 0/40 | 0/40 |
| `pre-s03-20260924T150809Z` | `f1a2b3c4d5e6` | 40 | 40 / 0 | 0/40 | 0/40 |
| `pre-s02-baseline-20260924T152107Z` | `f1a2b3c4d5e6` | 40 | 40 / 0 | 0/40 | 0/40 |
| `paytax-20260924T162726Z` | `f1a2b3c4d5e6` | 40 | 40 / 0 | 0/40 | 0/40 |
| `pre-cbr-20260924T171523Z` | `f1a2b3c4d5e6` | 40 | 40 / 0 | 0/40 | 0/40 |
| `pre-source-factory-20260925T010435Z` | `f1a2b3c4d5e6` | 40 | 40 / 0 | 0/40 | 0/40 |
| `20260925T052153Z` | `c8e3f1a6b904` | 1 | 1 / 0 | 0/40 | 0/40 |
| `data-recovery-01-20260925T055508Z` | `a2b3c4d5e6f7` | 100 | 90 / 10 | 0/40 | 0/40 |

The one-company `20260925T052153Z` dump is not an accepted operational recovery
point. A new protected recovery point was created and restore-rehearsed instead:

- path: `/home/mikhail/nextcompany-operational/backups/nextcompany_operational-data-recovery-01-20260925T055508Z.dump`
- SHA256: `f208192c7758af23195ee4902c166c679a08ee050960dd012da27780c4eb65b6`
- restore result: 100 companies, 90 legal entities, 10 IP, Alembic `a2b3c4d5e6f7`

## Master 40 → 100

The operation was `ADD 60`, not replacement:

- before: 40;
- after: 100;
- intersection: 40;
- removed: 0;
- added: 60 (50 legal entities and 10 IP).

The publication operator required exactly 40 existing rows, rejected candidate
INN/OGRN collisions, and inserted 60 rows in one transaction. Its post-verifier
recorded 90 legal entities and 10 IP.

## Replacement cohort

`docs/releases/public-v1-cohort-40.json` was selected only from the current
verified operational Master. The deterministic policy filters official
`fns_egrul` legal entities with valid identity and production-exportable
Risk/Summary, then ranks by usable persisted source coverage descending and INN
ascending. Risk outcome is never a selection input.

The manifest has SHA256
`8f44d69ad716b3481f92287e32df1c51f41aeecca5bd449265520d7656f40752`.
The operational gate is Master 40/40 and `build_projection()` 40/40.

# Stage 1.5 H — Company Fact Normalization

Date: 2026-09-17

Status: **FOUNDATION ACCEPTED / SOURCE COVERAGE PARTIAL**

## Model

Migration `b2c3d4e5f6a7` creates `company_public_facts`, an evidence-backed store for:

- website, phone, email;
- registered, factual, and postal address;
- mass address, mass director, and mass founder;
- publicly disclosed bank details;
- related company.

Each fact retains a canonical value hash, source/dataset identifiers, source URL, publication/effective dates when known, currentness, confidence, evidence, and observation time. Absence of a row is never a negative fact.

The uniqueness contract deduplicates the same company/type/value/source/source-record combination while preserving distinct evidence from different sources.

## Accepted source mapping

The PRIME disclosure adapter currently maps issuer-declared legal and postal addresses into `registered_address` and `postal_address`. Both facts retain exact-INN matching evidence and use:

- `confidence=source_asserted`;
- `currentness=observed_unverified_current`.

This wording prevents a disclosed address from being presented as independently verified or guaranteed current. Future official-registry and disclosure adapters may add corroborating facts; source precedence belongs in later projection logic, not destructive overwrites.

## Verification

| Check | Result | Evidence |
|---|---|---|
| Vocabulary/hash tests | PASS | Complete approved vocabulary; canonical JSON hash is key-order stable and value-sensitive; full suite: 511 passed |
| Migration | PASS | PostgreSQL head `b2c3d4e5f6a7` |
| Real write/read | PASS | AVTOVAZ produced two persisted facts: registered and postal address, both with exact-INN evidence and PRIME URL |
| Dedup/update contract | PASS BY DB CONSTRAINT | Named unique constraint plus PostgreSQL upsert refreshes observation/evidence without duplicating identical facts |
| Public-data boundary | PASS | Only company/public issuer fields are normalized; no private-person projection |

## Remaining source coverage

The schema foundation is ready, but source coverage is intentionally partial:

- website/phone/email need accepted source mappings;
- mass-address/director/founder require a lawful authoritative source and precise semantics;
- public bank details must come from an explicit disclosure and must never be labelled the only/current/active or blocked account without separate evidence;
- related-company facts require relationship type, dates, source evidence, and personal-data review where natural persons are involved.

These gaps do not justify inferred facts or false `not_found` states. They remain follow-up source work before later Risk/Summary conclusions use them.

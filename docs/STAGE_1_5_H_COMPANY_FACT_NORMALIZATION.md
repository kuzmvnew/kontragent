# Stage 1.5 H — Company Fact Normalization

Date: 2026-09-17

Status: **COMPLETE FOUNDATION / SOURCE COVERAGE PARTIAL**

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

## Address concentration context

The Master Registry now supplies a deterministic legal-address normalizer and an exact-full-address active-company count. The derived `mass_address` fact retains the original address, normalized address, building-level key, exact active count, coverage, source and non-negative interpretation.

Real AVTOVAZ example: exact full legal address active count is `1`. Building-level and room/office counts remain explicit `null` because a complete precomputed address index does not yet exist; they are not guessed. A business centre or shared building is never automatically negative.

Contacts, bank details and relationships already have evidence-backed model types. PRIME is mapped only where the issuer page actually supplies a company field; the provider support phone/email in the page footer are correctly excluded. No bank account is labelled primary, only, active or blocked. Director/founder name-only linkage remains `manual_review/unavailable`, not a strong fact.

## Verification

| Check | Result | Evidence |
|---|---|---|
| Vocabulary/hash/address tests | PASS | Complete approved vocabulary; canonical hash and deterministic address normalization |
| Migration | PASS | PostgreSQL head `b2c3d4e5f6a7` |
| Real write/read | PASS | AVTOVAZ has registered/postal PRIME facts plus derived Master Registry address context |
| Dedup/update contract | PASS BY DB CONSTRAINT | Named unique constraint plus PostgreSQL upsert refreshes observation/evidence without duplicating identical facts |
| Public-data boundary | PASS | Only company/public issuer fields are normalized; no private-person projection |

## Remaining source coverage

The foundation is complete, but source coverage remains intentionally partial:

- website/phone/email need accepted source mappings;
- building/room address counts need a precomputed normalized-address index; mass director/founder need stable identifiers;
- public bank details must come from an explicit disclosure and must never be labelled the only/current/active or blocked account without separate evidence;
- related-company facts require relationship type, dates, source evidence, and personal-data review where natural persons are involved.

These gaps do not justify inferred facts or false `not_found` states. They remain follow-up source work before later Risk/Summary conclusions use them.

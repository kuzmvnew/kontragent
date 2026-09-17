# Stage 1.5 D — Bankruptcy / Liquidation Event Semantics

Date: 2026-09-17

Status: **ACCEPTED — SEMANTIC FOUNDATION**

## Scope accepted

The project now has a normalized, evidence-backed `company_legal_events` model instead of a `bankrupt=true/false` flag. It distinguishes 15 states across bankruptcy intent/application/procedure stages, liquidation, planned exclusion, and actual exclusion.

Every stored event requires company, type, event date, status, source code, source identifier, source URL, evidence, check time, and retrieval time. Publication date, raw source type, and dataset link are retained when available.

The product projection includes explicit interpretation text. In particular:

- intent does not prove an application or a bankruptcy procedure;
- a filed application does not prove acceptance or an introduced procedure;
- an accepted application does not prove an introduced procedure;
- liquidation is not bankruptcy;
- planned/actual EGRUL exclusion is not automatically bankruptcy or liquidation;
- terminated/completed proceedings are not presented as currently active.

## Verification

| Check | Result | Evidence |
|---|---|---|
| Automated tests | PASS | 503 passed; semantic tests cover all 15 types and fail closed on unknown types |
| Migration | PASS | `e7a8b9c0d1e2 -> f8b9c0d1e2f3`; transactional PostgreSQL DDL |
| PostgreSQL write/read | PASS | rollback-only smoke insert/read for exact-INN company `0267007991`; evidence and type read back; 0 acceptance rows remain |
| Product projection | PASS | company page renders from migrated database without error; legal-event block is conditional and template compiles |
| Real bankruptcy source | N/A | FSSP and Fedresurs recovery inventories found no source implementation; this workstream adds semantics, not unsupported events |
| Counts/dates/coverage | N/A | table contains no fabricated production events; future adapters must establish their own coverage |
| Auto-update | NOT_CONFIGURED | intentionally deferred to the next approved stage |

## Source boundary

No source was fabricated or silently substituted. The new table is empty until an accepted adapter supplies evidence. The FSSP and Fedresurs inventory results remain `NOT_FOUND`; their unavailable source paths do not become `not_found` company results.

Every future adapter that writes these events must separately pass source access review and the six gates. The database constraint rejects event types outside the approved vocabulary, while the service also fails closed before persistence/projection.

## Limitations

- No real bankruptcy/liquidation feed is accepted by this workstream.
- No negative company check is emitted from an empty table.
- No current bankruptcy conclusion is derived from Master Registry `status` or `termination_date`, because those fields do not by themselves prove a bankruptcy event type.

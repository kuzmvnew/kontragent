# Stage 1.5 G — Corporate Disclosure Foundation

Date: 2026-09-17

Status: **ACCEPTED — SIX GATES PASS FOR PRIME FOUNDATION**

## Accepted implementation

The project has a free/public, exact-INN PRIME disclosure adapter with dated PostgreSQL cache, conservative four-state semantics, company-card/API refresh routes, and a visible source-attributed product block. Migration `a1c2d3e4f5b6` creates `corporate_disclosure_checks`.

The adapter stores issuer profile data and at most 100 document metadata/link records. It does not download or republish document contents. Access is targeted, cache-first, low-load, and fail-closed on protection or response mismatch.

## Verification

| Gate | Result | Evidence |
|---|---|---|
| Automated tests | PASS | Parser/product tests cover exact-INN found, explicit empty, mismatched response, identifier scope, document metadata/date extraction, and aggregation; full suite: 511 passed |
| Real public result | PASS | PRIME returned exact-INN profiles for `6320002223` (АО «АВТОВАЗ») and `7603000162`; a separate live page `6320004911` exposed 31 document metadata rows during source research |
| PostgreSQL write/read | PASS | Three dated checks read back: two `found`, one explicit `not_found`; AVTOVAZ exact INN/OGRN persisted |
| State semantics | PASS | `found`, explicit dated `not_found`, `not_applicable`, and fail-closed `unavailable` are distinct |
| Real Chromium/product | PASS | `/company/6320002223` displayed issuer name, exact INN/OGRN, disclosed address, count, and PRIME source link |
| Counts/dates/coverage | PASS WITH LIMITS | Check date retained; document count retained; coverage explicitly limited to issuers present at PRIME and recognized page rows |

## Access and rights decision

The source is approved only for targeted retrieval and source-attributed facts/metadata/links under the boundary in `PRIME_CORPORATE_DISCLOSURE_SOURCE_PASSPORT.md`. No bulk-crawl, document-mirroring, or broad republication right is assumed. Interfax and AK&M remain fallback candidates, not active adapters.

## Limitations

- PRIME is a disclosure distributor, not the universal company registry.
- `not_found` is not an adverse fact and does not prove that a company has no disclosure elsewhere.
- A source-asserted address is not guaranteed current.
- Auto-update is `NOT_CONFIGURED`; this checkpoint is on-demand/cache-first.
- Publicly disclosed bank details are supported by the normalized fact vocabulary but were not present in the accepted PRIME examples and must not be fabricated.

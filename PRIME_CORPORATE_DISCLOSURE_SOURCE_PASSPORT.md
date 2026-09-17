# PRIME corporate disclosure — source passport

Date: 2026-09-17

Status: **APPROVED FREE PUBLIC / TARGETED LOW-LOAD**

## Source and scope

- Provider: PRIME public issuer-disclosure portal.
- Company URL: `https://disclosure.1prime.ru/Portal/Default.aspx?emId={inn}`.
- Matching: exact 10-digit legal-entity INN confirmed by the INN returned on the issuer page. A page without the same INN is `unavailable`, not a match.
- Returned scope: issuer identity/profile plus metadata and links for disclosed documents when present.
- This is a public disclosure distributor, not an official company registry and not universal coverage of Russian legal entities.

## Access and use boundary

The adapter performs one targeted company-page request, caches the dated result, uses ordinary HTTP, and backs off on 403/429. It does not crawl identifier ranges, solve CAPTCHA, rotate proxies, spoof fingerprints, or bypass source protection.

The reviewed public surface did not provide a reliable machine-use licence or a grant to republish document contents. Kontragent therefore stores and displays only source-attributed issuer facts, document metadata, and source links. It does not mirror or republish documents. Bulk collection, redistribution, and broader commercial republication rights are not assumed.

If PRIME blocks the route or changes the page contract, the result is `unavailable`. Candidate fallback adapters remain Interfax disclosure and AK&M disclosure, each subject to a separate source passport, matching review, and Six Gates before use.

## Data contract

Stored profile fields:

- INN, OGRN, full/short issuer name;
- legal and postal addresses;
- registration date/authority;
- FSFR code and OKPO;
- source URL, request date, HTTP status, check/update timestamps.

Stored document fields are limited to title, publication date, and source URL (maximum 100 metadata records per company page). The adapter does not claim a complete document archive when the page contains no recognized document rows.

## Semantics

- `found`: the page explicitly returned the requested exact INN.
- `not_found`: PRIME returned its explicit “invalid issuer ID or no data” marker for the requested INN on the recorded date.
- `not_applicable`: the identifier is not a 10-digit legal-entity INN.
- `unavailable`: protection, timeout, network/HTTP/encoding error, schema mismatch, or a page that does not confirm exact INN.

Absence from PRIME is not a negative company signal. It means only that the selected disclosure distributor did not expose an issuer page for that exact INN at the check date.

## Currentness, privacy, and product projection

Profile fields are source-asserted observations, not independently verified current registry facts. Normalized address facts therefore use `currentness=observed_unverified_current`, retain exact-INN evidence and source URL, and must not silently overwrite a more authoritative registry fact.

Only company/public issuer information is projected. Any future extraction involving natural persons must receive a separate minimisation and public-display review.

## Operational policy

- cache-first, one exact-INN lookup on demand;
- default reuse of a successful dated cache entry;
- 30-second timeout and ordinary redirects;
- immediate fail-closed response on 403/429;
- no mass crawl and no auto-update claim at this stage;
- parser/schema changes must produce `unavailable`, never false `not_found`.

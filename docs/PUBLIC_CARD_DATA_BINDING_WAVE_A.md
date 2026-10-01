# MAC-OFFLINE-WAVE-A-PUBLIC-CARD-DATA-BINDING-01

Status: **IMPLEMENTATION CANDIDATE / QA PENDING**

Base: `5953e0bcaa778a18641329475e5a3ca1a027c52c`

This document records the semantic boundary implemented by the Mac offline
workstream. It is not HOME/production acceptance.

## Stable path

```text
Master Company
+ accepted source data
+ persisted Risk v3
+ persisted Summary v3
        ↓
CompanyViewModelV1 / company-view-v1
        ↓
PublicCompanyViewV1 / PublicProjection
        ↓
/api/company/{inn}
        ↓
/companies/{inn}
```

The public application never reads RAW artifacts, parser models, provider
payloads, operational job objects or Firmoteka-specific JSON. Public HTML and
the public API consume the same prepared `PublicProjection`.

## Canonical sections

`company-view-v1` now has stable sections for:

- identity, status, registration, address and activity;
- management, founders, contacts, capital and connections;
- finances, employees and tax;
- enforcement and licenses;
- courts, bankruptcy, procurement/RNP, restrictions and inspections;
- events, Risk, Summary, source coverage, freshness and limitations.

An empty domain is not a positive result. If no accepted evidence exists, its
section remains `NOT_CHECKED`. Source failure, stale data, unknown state and
not-applicability are separate states.

## Source authority matrix

| Semantic domain / field | Accepted official authority | Authorized bridge fallback | Current binding |
|---|---|---|---|
| identity / status / registration / address / OKVED | Master Registry (official master evidence) | Firmoteka | official wins; bridge retained as alternative when present |
| management / founders / capital | Master Registry where present | Firmoteka | official wins; public minimization applies |
| contacts | no official authority accepted in this slice | Firmoteka evidence | only explicitly corporate contacts are public; personal/unknown are authenticated-only |
| finance revenue / expenses / profit-loss | FNS REVEXP | Firmoteka | official wins |
| net profit / equity | GIRBO when accepted | Firmoteka | bridge fallback until accepted official fact exists |
| employees | FNS headcount | Firmoteka | official wins |
| taxes paid | FNS PAYTAX | Firmoteka | official wins |
| tax debt | FNS DEBTAM | Firmoteka | official wins |
| tax offences | FNS TAXOFFENCE | none | official only |
| enforcement | FSSP when accepted | Firmoteka | bridge fallback in current data path |
| licenses | Roszdrav license registry | Firmoteka | official wins |
| courts | official Moscow general-court integration, scope-limited | none | scoped aggregate/state only; Checko arbitration is not published as official |
| bankruptcy | Fedresurs target authority | none | no accepted live semantic binding yet: `NOT_CHECKED` |
| procurement / RNP | EIS RNP target authority | none | no persisted accepted result yet: `NOT_CHECKED` |
| restrictions | Bank of Russia warning list | none | exact-INN official binding; not a generic sanctions check |
| inspections | ERKNM | none | exact INN / exact OGRN fallback when source INN is absent |
| connections | authority follows management/founder evidence | Firmoteka when it is the selected basis | derived provider-independent relation |
| Risk | persisted Risk v3 | none | server-side compiled; no numeric NEXT Index |
| Summary | persisted Summary v3 | none | server-side compiled |

`AUTHORIZED_BRIDGE` always ranks below official primary/open-data evidence.
Firmoteka is publicly labelled **“Firmoteka · авторизованный вторичный
источник”** and **“Авторизованный вторичный источник”**. It is never labelled
as an official source.

## State semantics

- `FOUND`: positive/current accepted evidence exists.
- `NOT_FOUND`: only a completed, scoped negative observation. Its limitation
  text defines the exact coverage; it must not be generalized.
- `STALE_DATA`: a retained fact whose accepted freshness boundary has expired.
  The fact remains visible with its source date and limitation.
- `SOURCE_UNAVAILABLE`: the source/check could not establish a current result;
  no negative inference is allowed.
- `UNKNOWN`: evidence exists but does not establish the requested semantic
  result.
- `NOT_CHECKED`: no accepted check/evidence has been executed for that
  semantic domain.
- `NOT_APPLICABLE`: allowed only for fields whose field policy explicitly
  permits an applicability decision.
- `CONFLICTING_EVIDENCE`: selected authority and retained alternatives differ
  materially; alternatives are preserved rather than silently discarded.

Section state is the strongest limiting state of its facts. A stale,
unavailable, parsing-error or conflicting fact therefore cannot be hidden by a
separate `FOUND` fact in the same section.

## New official bindings

### ERKNM

The view reads only whitelisted normalized columns from
`erknm_inspections`. RAW JSON attributes, inspector objects and subject
provider payloads are never passed to the public contract. No-match semantics
are explicitly limited to already loaded periods.

### Bank of Russia warning list

The view uses exact INN on the active dataset snapshot and emits only a
whitelist of human fields. `raw_payload`, private/provider metadata and
unneeded addresses/sites are not forwarded. Absence applies only to this
warning list and must not be described as a completed sanctions check.

### Courts

The current public binding uses only the official Moscow general-court
integration and exports a scoped aggregate/state. It does not export provider
case JSON. A failed check is `SOURCE_UNAVAILABLE`. A successful zero-result
check is explicitly limited to the integration's actual coverage.

The existing Checko arbitration path is not bound to this public authority
because this task has no accepted decision authorizing it as an official
source.

## Privacy / public minimization

Public recursive validation continues to reject operational/internal fields,
including `company_id`, RAW/artifact/job fields, credentials, passports,
home/residential/registration-address private fields and private filesystem
paths. Public filtering also removes person INN/OGRNIP identifiers from
management/founder values and excludes personal/unknown phone/email contacts;
those remain available only to non-public audiences when permitted.

Provider records for ERKNM and the Bank of Russia are converted through
explicit allowlists. Firmoteka remains a field-level authorized bridge rather
than a provider-shaped public object.

## API and Card

`GET /api/company/{inn}` and SSR `/companies/{inn}` read one
`PublicProjection` revision. Card V2 renders semantic states and attribution
for connections, courts, bankruptcy, procurement/RNP, restrictions and
inspections, in addition to the existing company, finance, tax, enforcement
and license sections.

Requisites copy is implemented by a same-origin static script. CSP permits
`script-src 'self'` only; inline and third-party script execution is not
enabled.

## Deliberately not claimed

- no HOME deploy;
- no production DB mutation;
- no production source acceptance;
- no Fedresurs/RNP live acceptance;
- no generic sanctions-source coverage;
- no production crawl;
- no NEXT numeric index;
- no statement that implementation is accepted before QA/CI evidence.

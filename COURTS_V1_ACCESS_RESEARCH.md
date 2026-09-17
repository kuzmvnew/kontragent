# Arbitration Courts v1 — access research

Date: 2026-09-17

Scope: machine-readable Russian commercial-court cases for company cards. This is an access and licensing review, not approval of a paid dependency. No purchase, subscription, contract, payment details, account registration, CAPTCHA solving, stealth, proxy rotation, fingerprint evasion, or protection bypass was performed.

## Result

**C1 status: `PAID ACCESS DECISION REQUIRED`.**

No candidate currently qualifies as `APPROVED_FREE_OFFICIAL` or `APPROVED_FREE_PUBLIC` for the required production scope. The official KAD is the source of truth and works interactively, but no documented public machine API was found and ordinary direct/headless access was blocked. Commercial feeds exist. Checko advertises a free API tier, but its published court schema is materially narrower than C1 and public terms do not establish the storage, cache, commercial display, or republication rights required by Kontragent.

No provider was added as a production dependency and no trial account was opened.

## Status vocabulary

| Status | Meaning in this review |
|---|---|
| `APPROVED_FREE_OFFICIAL` | Official, free, documented machine channel with adequate rights and coverage. |
| `APPROVED_FREE_PUBLIC` | Non-official but free/public machine channel with adequate rights and coverage. |
| `TRIAL_AVAILABLE` | A free/demo route is advertised, but it is time-, quota-, schema-, account-, or contract-limited. |
| `PAID_OPTION` | A real commercial integration exists; procurement and contract review are required. |
| `ACCESS_PENDING` | Credentials, current documentation, commercial terms, or rights must be obtained from the provider. |
| `BLOCKED` | The tested route is not a lawful/reliable production machine path. |

## Free official/public path re-check

### Official KAD

- Owner/channel: federal arbitration-court information system, listed by the courts themselves as the Automated Information System “Kartoteka arbitrazhnykh del”: <https://arbitr.ru/materials/ojfederal_nixjarbitrazhnixjsudax/list_is>.
- Public UI: <https://kad.arbitr.ru/Kad> supports party role, court, case number, and filing-date filters. It links the Bank of Decisions and exposes case cards/documents for human use.
- Exact-INN interactive check on 2026-09-17: INN `1215214540` returned 54 cases in ordinary Chromium.
- Machine check: the UI uses an undocumented internal `SearchInstances` call with CAPTCHA/`RecaptchaToken` support. One equivalent low-load direct POST returned HTTP 451 from DDoS protection. One ordinary headless Chromium attempt produced no results within 90 seconds.
- Official documentation search found no public API specification, key-registration procedure, bulk feed, rate limits, or machine-use licence for company case search.

Conclusion: `BLOCKED` for unattended production access. The interactive success is evidence that KAD is live, not permission or proof of a stable API.

### Other free/public candidates

Searches of official arbitration-court surfaces and the public documentation of the commercial providers below found no separate official open-data feed equivalent to KAD. Third-party sites are commercial aggregators, even where they have a zero-price quota. Therefore no route is classified `APPROVED_FREE_OFFICIAL` or `APPROVED_FREE_PUBLIC`.

## Candidate summary

| Candidate | Provider / channel | Underlying source and proximity to KAD | Docs | Access status |
|---|---|---|---|---|
| KAD | Federal arbitration courts; official | Primary official source of truth | Public UI/manual only; no documented public machine API found | `BLOCKED` |
| Casebook API / ПравоДанные | PravoTech; commercial aggregator | Judicial data/monitoring product built around court records; close commercial substitute, not an official court channel | [API v3 documentation index](https://pravo.tech/documents/casebook-api) | `PAID_OPTION`, `ACCESS_PENDING` |
| Контур.Фокус API | SKB Kontur; commercial aggregator | Provider explicitly names arbitration courts among official sources; not an official court channel | [API demo/docs entry](https://focus.kontur.ru/site/demo/requisites) | `PAID_OPTION`, `ACCESS_PENDING` |
| Checko API `/v2/legal-cases` | ООО «Дата Максимум»; commercial aggregator | Returns KAD links and court-derived records, but documents a 1–2 week arbitration-data delay; not official | [API](https://checko.ru/integration/api), [legal cases method](https://checko.ru/integration/api/legal-cases) | `TRIAL_AVAILABLE`, `ACCESS_PENDING` |
| СПАРК API | Интерфакс; commercial aggregator | Aggregates arbitration-court information; reports >40,000 incoming court documents/day | [integration](https://spark-interfax.ru/integration), [arbitration](https://spark-interfax.ru/features/arbitration-proceedings) | `PAID_OPTION`, `ACCESS_PENDING` |
| Seldon.Basis.API | ООО «Селдон 2»; commercial aggregator | Provider says official-source changes flow into its data and API includes arbitration cases; not official | [API](https://basis.myseldon.com/ru/home/api) | `PAID_OPTION`, `ACCESS_PENDING` |

## Published field coverage

Legend: `Y` published support; `P` partial/derived/plan-dependent; `N` absent in the published method; `U` not confirmed in public API documentation.

| Field / capability | KAD UI | Casebook API | Focus API | Checko legal-cases | SPARK API | Seldon API |
|---|---:|---:|---:|---:|---:|---:|
| Exact INN / OGRN company lookup | P / P | Y | Y | Y | Y | Y |
| Case number | Y | Y | P | Y | Y | U |
| Parties | Y | Y | P | Y | Y | U |
| Claimant / defendant roles | Y | Y | P | Y | Y | P |
| Third-party role | Y | P | U | N | U | U |
| Filing date | Y | Y | P | Y | Y | U |
| Court | Y | Y | P | Y | Y | U |
| Claim amount | Y | Y | P | Y | Y | Y |
| Awarded amount | P | U | U | N | P | U |
| Stage / status | Y | Y | P | P (`actual`/`active`) | Y | P |
| Judicial acts / documents | Y | Y | U | N (KAD link only) | Y | U |
| Historical cases / process history | Y | Y | P | P (case list, no process history) | Y | P |

`P` for KAD exact identifiers means the public party filter accepted and returned the exact-INN test, but no documented OGRN/exact machine contract exists. For commercial products, public marketing UI capability is not assumed to be API schema: where current API method documentation was unavailable, the value remains `U`.

## Commercial, operational, and rights comparison

| Candidate | Update / history | Published pricing and trial | Limits | Storage / cache / commercial use / display | SLA and personal data |
|---|---|---|---|---|---|
| KAD | Live public case system and historical cards | Free human UI | Undocumented; protected by CAPTCHA/DDoS controls | No public machine licence or republication grant found | No machine SLA found; statutory publication restrictions still apply |
| Casebook API | Monitoring and judicial history are product features | Published API price list: 5,000 requests/month from 588,600 RUB/year; 10,000 from 1,112,264 RUB/year. No self-serve API trial confirmed | Published ceiling 400 requests/min; monthly tier quota | Not granted by the public documentation index; contract review required for storage, cache, derived facts, public card display, and republication | PravoTech publishes support and personal-data policies, but API SLA/data-processing allocation must be confirmed in contract |
| Focus API | Provider uses official sources; exact arbitration refresh/history terms not public on the reviewed page | Demo request advertised, but the public demo says it has ended and offers examples/application. Current court-API price not publicly established | Current court-method limits not confirmed | Licence/terms review required; no public grant identified for permanent storage or public republication | SLA and personal-data allocation require commercial documents |
| Checko API | General API says daily updates; arbitration docs say delay may be 1–2 weeks. Historical case list, no act history | Free `Light` plan advertised for registered users: all methods, up to 100 requests/day. Paid per-request plans are also advertised | 100/day on Light; legal-cases page size up to 100 | Public pages do not clearly grant production caching, long-term storage, commercial public display, or republication; registration/key and terms review required | No court-specific SLA found; party names/addresses may include personal data, so public projection needs minimisation and legal review |
| SPARK API | Daily aggregation; >40,000 court documents/day; process history and monitoring | Price not public. UI demo: free view of 5 companies, corporate email and IP binding; this is not a confirmed API key trial | 100 requests/sec/client and 25m/day platform figures published; contract quotas may differ | Integration page explicitly mentions creating own stores with full data, but public display/republication scope still requires the licence | Reliability claims are marketing, not a quantified SLA; personal-data rules and permitted projections require contract review |
| Seldon API | Claims changes appear when reflected in government sources; history/monitoring exists in product | API connection requires contacting provider/application. Site product tariffs exist, but API price is personalised. A free one-day product access is advertised, not a confirmed unrestricted API trial | Not public | Public offer is an end-user software licence and does not establish API cache/republication rights; separate API terms required | Support says responses within a day, not API uptime SLA; personal-data consent applies to applicants and data use needs contract review |

Prices are vendor-published list information observed on 2026-09-17, not quotations. VAT, tier inclusions, document traffic, monitoring calls, and public-display rights can change and must be confirmed directly before any decision.

## Minimal trial/probe decision

No commercial probe was run:

- KAD was already tested without bypass and is blocked as a machine path.
- Checko's free tier requires registration and an API key; the reviewed public pages did not make the required cache/storage/commercial display/republication rights clear, and its court method cannot supply third parties, awarded amounts, acts, or process history.
- Focus and SPARK offer application-based demos rather than an anonymous/self-serve court API key.
- Casebook and Seldon require provider contact/access terms.

Creating an account or submitting corporate/personal contact data would accept terms or start commercial engagement without resolving the decisive rights/coverage gaps. That is not necessary for this technical conclusion and was not done.

## Procurement questions required before approval

For any shortlisted provider, obtain a written answer and contract exhibit for:

1. exact request/response schema and a sandbox response for INN, OGRN, case number, every party role, dates, court, claimed/awarded amounts, stage, acts, and documents;
2. KAD/court source lineage, refresh latency, corrections, deletion, and full-history availability;
3. request, concurrency, monthly-object, document-download, monitoring, and overage limits;
4. rights to persist raw responses and normalized facts, cache duration, re-check cadence, backups, and termination handling;
5. rights to use the data commercially in Kontragent, show facts to end users, publish company-card/SEO facts, quote acts, and expose results through an API/report;
6. uptime/support SLA, incident notice, versioning/deprecation, and data-quality remedies;
7. personal-data roles, lawful-basis allocation, suppression/expiry requirements, locality, subprocessors, and incident duties;
8. annual price including sandbox, production, history, acts/documents, monitoring, public-display/republication rights, VAT, and overages.

## C1 decision

No option is approved. C1 remains open as **`PAID ACCESS DECISION REQUIRED`**. If procurement is later authorised, Casebook API and SPARK are the strongest full-history candidates on published capability; Checko is a low-cost schema/prototype candidate only and does not meet the present C1 completion criteria. A provider can pass C1 only after the contractual rights above are confirmed and the implementation passes all Six Gates.

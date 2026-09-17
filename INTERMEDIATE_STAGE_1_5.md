# INTERMEDIATE STAGE 1.5 — Kontragent

Status: CLOSED / ACCEPTED
Date: 2026-09-17

## Purpose

Stage 1.5 is the mandatory bridge between the officially closed Wave 1 and the full Risk Engine / Summary Engine / Company Card v2 work.

The goal is not to start Wave 2 early. The goal is to make sure the risk model is built on a minimally sufficient, verified and free-source company-data foundation rather than on a partial set of tax/licence datasets.

Wave 1 is officially closed by `WAVE1_CLOSURE_DECISION.md`.

## Execution rule

Do not start Wave 2 during Stage 1.5.

Do not rewrite sources that already exist in current, historical or local project state. Inventory first, then reuse/migrate verified implementation.

Every source or returned legacy integration must keep the permanent six-gate acceptance protocol:

1. automated tests;
2. real official/public-source result;
3. PostgreSQL write/readback;
4. correct `found / not_found / not_applicable / unavailable` semantics;
5. real browser/product verification;
6. counts, dates and coverage.

## Workstream A — close unfinished acceptance of pre-Wave1 tax sources

### A1. FNS Tax Debt

Current state: ingestion/product foundation exists, but final acceptance is still not closed in `PROJECT_STATUS.md`.

Required Stage 1.5 work:

- confirm real source file/snapshot and parser result;
- confirm debt totals and itemisation;
- preserve separate tax / penalties / fines where the source provides them;
- verify data date and source freshness semantics;
- verify `found`;
- verify dated `not_found` only inside real checked coverage;
- verify `not_applicable` where relevant;
- verify `unavailable` on source/data failure;
- PostgreSQL write/readback;
- real company card in Chromium;
- record counts, dates and Master Registry coverage;
- verify history if stored;
- verify that source failure never becomes “no debt”.

Also verify the product distinction between ordinary published tax debt and the separate FNS fact about debt over the applicable threshold that has been sent for enforcement to a bailiff where that dataset is available.

### A2. FNS Tax Offences

Current state: code/product foundation exists, but final state/browser acceptance is still open.

Required Stage 1.5 work:

- real official record;
- real `found` company;
- real `not_found` scenario inside known coverage;
- correct dates and amounts;
- PostgreSQL readback;
- browser block;
- count/date/coverage audit;
- no false “clean” conclusion if data is unavailable.

Stage 1.5 cannot be closed while A1/A2 remain unaccepted.

## Workstream B — inventory already-developed FSSP and Fedresurs work

The product owner states that FSSP and Fedresurs were already developed earlier.

Before creating any new code:

- search current `main`;
- search historical branches/commits;
- inspect old/local project files if needed;
- inspect PostgreSQL tables/migrations;
- inspect providers/services/ingestion/tests/UI;
- identify what can be reused;
- classify result as `READY / PARTIAL / LEGACY_MIGRATION / NOT_FOUND`.

Do not duplicate working implementation.

## Workstream C — Courts v1 before Risk Engine

### C1. Arbitration courts

Target: official/public arbitration case data with a low-load on-demand/cache approach where no approved bulk channel exists.

Minimum Court v1 model:

- case number;
- court;
- parties;
- role: claimant / defendant / third party / other;
- filing date;
- stage/instance;
- hearings/timeline where available;
- claim amount where available;
- awarded amount where reliably extractable;
- active/completed state;
- source URL/identifier;
- judicial act/document metadata;
- source/date/evidence.

Derived facts for later Risk Engine:

- active defendant case count;
- active claims amount;
- claims/revenue ratio when revenue exists;
- largest claim/revenue;
- new cases over period;
- case-count growth;
- actual awarded/lost amount where confirmed.

Court claim amount is never treated as confirmed debt.

### C2. Courts of general jurisdiction

Build the same core semantics where legally and technically available through official/public court systems.

Court v1 is required before the full Risk Engine. Deep document intelligence and prediction of case outcomes remain later Court v2/v3 work.

## Workstream D — Bankruptcy and liquidation event model

Do not model bankruptcy as a single boolean.

Minimum event states must distinguish where supported by evidence:

- intent to file for bankruptcy;
- bankruptcy application filed;
- application accepted by court;
- observation/supervision or other introduced procedure;
- financial rehabilitation/external administration where applicable;
- restructuring/realisation for applicable person/IP scenarios;
- liquidation/bankruptcy estate stage;
- procedure terminated/completed;
- company liquidation decision;
- liquidation process;
- planned exclusion / actual exclusion from EGRUL where applicable.

Liquidation is not automatically bankruptcy.

Summary examples must use plain language, for example:

- “Компания находится в процессе ликвидации. Введённая процедура банкротства не подтверждена.”
- “Опубликовано намерение обратиться с заявлением о банкротстве. Само дело о банкротстве пока не подтверждено.”
- “Арбитражный суд принял заявление о признании компании банкротом, но процедура банкротства ещё не введена.”
- “Суд ввёл процедуру наблюдения.”

Each statement must retain event date, source and evidence.

## Workstream E — FNS bank-account suspension decisions

Add a separate company check for FNS decisions suspending operations on bank accounts.

Required product facts where the public service provides them:

- whether active suspension decisions exist;
- decision number;
- decision date;
- tax authority;
- stated reason;
- bank BIC where returned;
- source check date.

Do not claim a specific bank account number is blocked unless the source directly proves that account.

Run a technical experiment with multiple BIC values for a known company to determine exact public-service query semantics before finalising the provider contract.

## Workstream F — Bank of Russia KYC platform / high-risk flag technical probe

The public check is a high-priority regulatory source for the future Risk Engine.

Stage 1.5 task:

- inspect the public browser workflow;
- capture request/response semantics;
- determine what the public result actually proves;
- determine freshness/date semantics;
- determine cache policy;
- identify CAPTCHA/challenge role;
- test a stable low-load on-demand workflow;
- look for an approved machine endpoint before relying on browser automation;
- do not mislabel the public result as a full internal risk category if the public service only confirms high-risk information.

Product result must distinguish:

- high-risk information found;
- high-risk information not found after a successful check;
- check unavailable.

Stage 1.5 does not require stealth anti-bot evasion. The implementation must be stable enough for production and must stop/back off on protection or rate-limit signals.

## Workstream G — Free corporate disclosure foundation

Create one internal `corporate_disclosure` domain source with interchangeable public-source adapters.

Free-first policy:

- use public issuer/corporate-disclosure pages and public documents before considering paid feeds;
- search by exact INN/OGRN/issuer identifier where possible;
- cache already collected pages/documents;
- one worker / low request concurrency per domain;
- no mass sequential crawl when targeted company lookups are enough;
- back off on 403/429/protection errors;
- do not use proxy rotation/fingerprint evasion as normal ingestion architecture;
- store source URL and retrieval date.

Target facts when actually disclosed:

- issuer/company identity;
- corporate messages/significant facts;
- accounting/interim reports;
- affiliated-person lists;
- registrar information;
- charters and corporate documents;
- public contacts/addresses;
- publicly disclosed bank details;
- corporate events useful for monitoring/risk.

Publicly disclosed bank details must be labelled as disclosed details, not automatically as the company’s only/current/main account.

Detailed plan: `CORPORATE_DISCLOSURE_SOURCE_PLAN.md`.

## Workstream H — company-card facts needed by Risk/Summary

Before the final Company Card v2, normalise these fields where reliable sources exist:

- website(s);
- phone(s);
- email(s);
- registered/legal address;
- factual address only when independently supported;
- postal address where available;
- address-source/date;
- mass-address facts;
- mass-director facts;
- mass-founder facts;
- publicly disclosed bank details;
- related-company relationship reason and period.

Mass address/director/founder is a context signal, not an automatic guilt/fraud label.

For business centres, shopping centres, technoparks and similar multi-tenant locations, distinguish building-level concentration from exact-room/office coincidence and explain uncertainty.

## Completion criteria for Stage 1.5

Closure decision on 2026-09-17: all criteria below are satisfied or have the explicit safe disposition recorded in `STAGE_1_5_ACCEPTANCE.md`. C1 passed the agreed Checko free-bridge minimum. C2 is accepted with partial targeted coverage after bounded SPb/Sverdlovsk HTTP and visible-browser attempts stopped at source timeout/CAPTCHA without bypass.

Stage 1.5 is complete only when:

- Tax Debt final acceptance is closed;
- Tax Offences final acceptance is closed;
- FSSP/Fedresurs inventory is completed and any reusable implementation is classified/migrated as needed;
- Court v1 arbitration path is working at the agreed minimum scope;
- court-of-general-jurisdiction path is working or explicitly documented as technically unavailable with a safe fallback/status;
- bankruptcy/liquidation event semantics are implemented or the reusable legacy path is upgraded to that model;
- FNS account-suspension check is working;
- Bank of Russia high-risk public check technical probe is completed and its production mode is decided;
- free corporate-disclosure foundation is working for at least one reliable public source, with fallback research documented;
- all new/recovered checks preserve Evidence, date and availability semantics;
- no source error is converted into `not_found`.

## What comes next

After Stage 1.5:

1. Auto-update / Data Readiness;
2. full Risk Engine;
3. Summary Engine;
4. Company Card v2;
5. Report v1;
6. Security & Resilience Gate;
7. Legal Launch Gate;
8. Product / Company Card Acceptance Gate;
9. first 10,000 SEO company pages;
10. Lists + Bulk Check;
11. Monitoring / Event Engine;
12. Workspace v1;
13. only then Wave 2.

Wave 2 remains a later expansion stage, not part of Stage 1.5.

# PRODUCT / COMPANY CARD ACCEPTANCE GATE — Kontragent

Status: APPROVED MANDATORY PRE-SEO GATE
Date: 2026-09-17

The first 10,000 SEO company pages may be indexed only after this gate passes.

## 1. Goal

Verify not only that pages load, but that the whole product flow, company data, calculations, risk conclusions and summaries are correct and understandable.

A visually correct card with a wrong conclusion fails the gate.
A correct conclusion with broken navigation/authentication also fails the gate.

## 2. Full user journey

Test end-to-end:

- landing/home entry;
- company search;
- search by INN/OGRN/name where supported;
- search results;
- open company card;
- all tabs/accordions/links/buttons;
- internal navigation;
- URL/canonical behaviour;
- registration;
- login;
- logout;
- access recovery when implemented;
- authorised vs unauthorised states;
- paywall/closed-function states where applicable;
- error and empty states;
- desktop and mobile;
- supported browsers;
- loading/failure states.

## 3. Data correctness chain

For selected real companies verify the complete chain:

`official source -> ingestion/cache -> PostgreSQL -> Evidence -> Fact -> Derived Metric -> Risk Rule -> Signal -> Summary -> UI/report`

For each tested company confirm:

- correct entity;
- correct INN/OGRN and basic identifiers;
- correct status;
- correct dates;
- correct amounts;
- correct source attribution;
- correct financial period;
- correct histories;
- no duplicated facts;
- expired/historical facts are not shown as current;
- source freshness is represented correctly.

## 4. State semantics acceptance

Mandatory checks:

- `found` really means a relevant record was found;
- `not_found` is used only after a successful applicable check within known coverage;
- `not_applicable` does not become a negative signal;
- `unavailable` never becomes “nothing found”;
- missing source coverage does not create a positive conclusion;
- user-visible wording reflects these states.

## 5. Risk/Summary acceptance

For every material conclusion answer:

“Why did the system write this?”

The answer must resolve to facts, sources, dates and calculations.

Example — enforcement:

- active enforcement amount: 8.0m RUB;
- revenue 2025: 20.0m RUB;
- ratio: 40%.

Expected summary may state that the amount is material relative to annual revenue under the current ruleset.

Example — courts:

- 7 active defendant cases;
- active claims: 120m RUB;
- revenue 2025: 80m RUB;
- claims/revenue: 150%.

Expected summary must also state that claimed amount is not confirmed debt.

No vague “large/significant/high” wording without supporting numbers when a quantitative rule is used.

## 6. Golden companies

Maintain a curated set of real “golden companies” with manually confirmed expected results.

The set should cover at least:

- large company;
- small company;
- LLC/legal entity;
- sole proprietor;
- new company without annual statements;
- data-rich company;
- data-poor company;
- procurement participant and non-participant;
- regulated/licensed company and ordinary company;
- company with no material signals;
- WARNING case;
- CRITICAL/hard-rule case;
- company with one or more unavailable sources;
- liquidated/reorganised company;
- difficult boundary cases.

After significant source/Risk/Summary changes, regression checks compare actual results against the accepted expectations.

Unexpected changes require explanation before release.

## 7. Automated test layers

Before 10k SEO run the relevant full suite:

- unit tests;
- integration tests;
- PostgreSQL tests;
- source/service/aggregator tests;
- browser end-to-end tests;
- regression tests;
- Risk Engine tests;
- Summary Engine tests;
- Derived Metrics arithmetic tests;
- Applicability tests;
- Coverage/state tests;
- authentication/access-control tests;
- public projection/privacy tests;
- report tests;
- SEO-card rendering tests;
- production-like smoke tests.

## 8. Arithmetic and wording tests

Test calculations independently, including:

- ratios;
- percentages;
- period comparisons;
- growth/decline;
- rounding;
- missing denominators;
- zero values;
- historical vs current periods;
- claims vs awarded amounts;
- forecast vs fact.

Summary test fixtures should verify that numbers shown to the user match the calculated values and source periods.

## 9. Source failure scenarios

Simulate or test:

- source timeout;
- source unavailable;
- partial coverage;
- stale data;
- malformed/changed source payload;
- missing reporting period;
- failed background update.

The card must degrade safely and must not invent clean/positive results.

## 10. Gate acceptance definition

This gate passes only when a randomly selected company from the accepted test set can be opened and trusted at three levels:

1. Product flow works.
2. Displayed facts and source dates are correct.
3. Risk/Summary conclusions are explainable and mathematically/logically correct.

Any critical failure blocks mass 10k SEO indexing until fixed and retested.

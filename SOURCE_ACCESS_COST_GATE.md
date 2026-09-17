# SOURCE ACCESS & COST GATE — Kontragent

Status: APPROVED MANDATORY GATE
Date: 2026-09-17

## 1. Purpose

Before implementing any new Stage 1.5 or Wave 2+ source, confirm that the data can be obtained, stored and used in our commercial product in the intended way.

Default policy: **free/public/official first**.

A paid source is not the normal starting point. It requires a separate explicit commercial decision after practical free-source options are checked.

A public website is not automatically an approved unlimited bulk source, but public targeted access may be a valid source mode when it is lawful, technically stable, low-load and consistent with the source rules.

## 2. Required source passport fields

For every source record:

- source owner/operator;
- official/public URL/documentation;
- dataset/registry name;
- source class: OPEN_DATA / PUBLIC_LIVE / ON_DEMAND / LICENSED / CONSENT / EXCLUDE;
- entity types;
- identifiers and matching rules;
- machine-readable mode, if any;
- browser/public-page mode, if any;
- registration/credential requirements;
- CAPTCHA/challenge/protection behaviour;
- rate limits / load restrictions;
- automation restrictions;
- storage/cache rights;
- retention period;
- expiry/purge requirements;
- republishing rights;
- commercial-use rights;
- public SEO display rights;
- authenticated-user display rights;
- export/API rights;
- personal-data restrictions;
- price and billing unit;
- contract/SLA requirements;
- update frequency;
- source freshness semantics;
- fallback policy;
- expected product workflows closed by the source;
- estimated implementation/operations cost;
- explicit unresolved questions.

## 3. Approval states

Recommended states:

- `APPROVED_FREE_OFFICIAL`;
- `APPROVED_FREE_PUBLIC`;
- `APPROVED_ON_DEMAND`;
- `APPROVED_PAID_BY_EXPLICIT_DECISION`;
- `ACCESS_PENDING`;
- `LEGAL_REVIEW_REQUIRED`;
- `TECHNICAL_ACCESS_UNCONFIRMED`;
- `BLOCKED`;
- `FUTURE_SOURCE`;
- `EXCLUDE`.

No production ingestion starts from an unresolved state unless a separately documented exception is approved.

## 4. Free-first decision order

For each source, check in this order:

1. official downloadable open data / bulk file;
2. official/public machine endpoint;
3. official/public company lookup page with targeted on-demand access;
4. other reliable public disclosure source carrying the same primary/publicly disclosed information;
5. browser-assisted low-load retrieval if the result is public and the mode is stable/acceptable;
6. only then consider a paid feed/API by separate explicit product decision.

Do not buy a paid API only because it is easier to integrate.

## 5. Low-load public-source collection rule

For targeted public pages where no bulk interface is available:

- prefer exact INN/OGRN/company lookup over sequential crawling;
- use one network worker / low concurrency per domain;
- use local cache and document reuse;
- update only when required by source freshness/product workflow;
- use reasonable delays and exponential backoff;
- stop/reduce load on 403/429/protection failures;
- log retrieval source/date;
- do not make proxy rotation, browser-fingerprint evasion or protection bypass the normal architecture.

If a public source is unstable or disallows the intended automation, use another approved source/fallback or mark the check unavailable.

## 6. Cost model

Even for free data, record operational cost:

`source fee + infrastructure + storage + traffic + development + maintenance + legal/licensing overhead`

For each source, estimate:

- one-time setup cost;
- monthly/annual external fee (normally 0 for Stage 1.5 target sources);
- storage growth;
- expected request volume;
- update cadence;
- operational support burden.

## 7. Product value test

A source should answer a concrete product need:

- company card;
- risk rule;
- monitoring event;
- bulk check;
- Person;
- Compliance;
- sector/context check;
- report/export;
- enterprise integration.

Do not add a registry merely because it exists.

## 8. Stage 1.5 source rule

Stage 1.5 is a prerequisite before the full Risk Engine, not Wave 2.

Its priority sources/tasks are defined in `INTERMEDIATE_STAGE_1_5.md` and should be implemented using free/public/official modes wherever practical.

This includes:

- Tax Debt / Tax Offences final acceptance;
- FSSP/Fedresurs inventory and reuse;
- courts;
- FNS account-suspension decisions;
- Bank of Russia public high-risk check;
- free corporate disclosure.

## 9. Corporate disclosure

Corporate disclosure is free-first.

Use public company/issuer disclosure pages/documents through interchangeable adapters before any paid API/FTP decision.

See `CORPORATE_DISCLOSURE_SOURCE_PLAN.md`.

Paid disclosure feeds are optional later accelerators, not required Stage 1.5 sources.

## 10. FSSP / Fedresurs inventory rule

The product owner has previously indicated that FSSP and Fedresurs may already exist in earlier/local project state.

Before any task named “connect FSSP” or “connect Fedresurs”:

1. search current `main` again;
2. inspect historical branches/commits if necessary;
3. check the user’s local project/database where relevant;
4. determine whether working code/data exists outside current `main`;
5. reuse/migrate verified implementation if valid;
6. do not duplicate an already working integration.

This is an inventory requirement, not a conclusion that the sources were never implemented anywhere.

## 11. Decision record

Every later Wave 2 source should have a short decision:

- why we need it now;
- which feature it unlocks;
- free-source option checked;
- access/legal status;
- cost;
- implementation scope;
- refresh model;
- acceptance criteria;
- decision: GO / WAIT / NO-GO.

A paid source cannot become a default dependency without a separate explicit decision by the product owner.

# SOURCE ACCESS & COST GATE — Kontragent

Status: APPROVED MANDATORY GATE
Date: 2026-09-17

## 1. Purpose

Before implementing any new Wave 2+ source, confirm that the data can be obtained, stored and used in our commercial product in the intended way and at an acceptable cost.

A public website is not automatically an approved machine-readable commercial source.

## 2. Required source passport fields

For every source record:

- source owner/operator;
- official URL/documentation;
- dataset/registry name;
- source class: OPEN_DATA / PUBLIC_LIVE / ON_DEMAND / LICENSED / CONSENT / EXCLUDE;
- entity types;
- identifiers and matching rules;
- official machine-readable mode;
- registration/credential requirements;
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
- `APPROVED_PAID`;
- `ACCESS_PENDING`;
- `QUOTE_REQUIRED`;
- `LEGAL_REVIEW_REQUIRED`;
- `TECHNICAL_ACCESS_UNCONFIRMED`;
- `BLOCKED`;
- `FUTURE_SOURCE`;
- `EXCLUDE`.

No production ingestion starts from an unresolved state unless a separately documented exception is approved.

## 4. Cost model

Do not evaluate only API price. Record total cost:

`source fee + infrastructure + storage + traffic + development + maintenance + legal/licensing overhead`

For each source, estimate:

- one-time setup cost;
- monthly/annual external fee;
- storage growth;
- expected request volume;
- update cadence;
- operational support burden.

## 5. Product value test

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

## 6. Current researched economic notes

These are planning notes as of 2026-09-17 and must be rechecked before purchase/signing:

- GIR BO full REST subscription was researched as a material paid source; purchase should be justified by product/financial-analysis needs rather than bought automatically.
- Fedresurs/EFРSB official service requires access/contract workflow; final production pricing/rights must be confirmed before relying on a paid channel.
- FSSP machine access requires fresh confirmation of the current legal/technical channel before new work.
- Judicial bulk/document data may become expensive if a licensed commercial feed is required; public browser availability is not enough.
- Rospatent open-data blocks are economically attractive when the official open-data licence/terms cover the intended use.
- EIS/RNP remains an official-access problem rather than a reason to replace Source of Truth with a temporary commercial mirror.

## 7. FSSP / Fedresurs inventory rule

The user has previously indicated that FSSP and Fedresurs may already exist in an earlier/local project state. Current `main` inspection on 2026-09-17 did not confirm dedicated FSSP/Fedresurs implementation paths in the active repository tree.

Therefore, before any Wave 2 task named “connect FSSP” or “connect Fedresurs”:

1. search current `main` again;
2. inspect historical branches/commits if necessary;
3. check the user’s local project/database where relevant;
4. determine whether working code/data exists outside current `main`;
5. reuse/migrate verified implementation if valid;
6. do not duplicate an already working integration.

This is an inventory requirement, not a conclusion that the sources were never implemented anywhere.

## 8. Decision record

Every Wave 2 source should have a short decision:

- why we need it now;
- which feature it unlocks;
- access/legal status;
- cost;
- implementation scope;
- refresh model;
- acceptance criteria;
- decision: GO / WAIT / NO-GO.

This decision is made before coding the source, not after months of integration work.

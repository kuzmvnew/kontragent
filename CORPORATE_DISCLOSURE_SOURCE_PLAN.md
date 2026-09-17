# CORPORATE DISCLOSURE SOURCE PLAN — Kontragent

Status: APPROVED RESEARCH / STAGE 1.5 INPUT
Date: 2026-09-17

## Goal

Add corporate disclosure as a free-first company-data domain before the full Risk Engine.

The product must not depend on buying a paid disclosure API for the first commercial Company Core if equivalent useful facts can be collected from reliable public disclosure pages/documents with targeted low-load access.

## Internal domain model

Use one logical source family:

`corporate_disclosure`

with multiple interchangeable adapters rather than coupling the product to one commercial vendor.

Candidate public disclosure systems include accredited/public Russian disclosure portals such as:

- Interfax disclosure public pages;
- PRIME disclosure public pages;
- AK&M disclosure public pages;
- other reliable accredited/public disclosure resources after source-passport review.

The adapter selected for a company may vary according to availability and completeness.

## Access strategy

Free-first targeted retrieval:

1. search by exact INN/OGRN or stable issuer identifier where available;
2. fetch only company pages/documents required for the requested company;
3. cache HTML/document metadata locally;
4. reuse cached documents instead of repeated downloads;
5. run one network worker / low concurrency per domain;
6. use reasonable request intervals and exponential backoff;
7. stop or reduce load on 403/429/protection failures;
8. avoid mass sequential crawling when direct company lookup is enough;
9. do not make proxy rotation, browser-fingerprint evasion or CAPTCHA breaking a normal ingestion dependency;
10. store retrieval time and original source URL.

If a source explicitly forbids automation or becomes unstable, use another approved public disclosure adapter or classify that sub-source unavailable.

## Evidence capture

Preferred order:

1. structured HTML fields;
2. downloadable official disclosure documents;
3. document metadata and text extraction;
4. browser/screenshot evidence only when a value is visually available but not reliably structured, or as an acceptance proof.

A screenshot is evidence, not the primary database format.

## Target company facts

Collect only when actually disclosed and attributable to the company:

- full/short company name;
- INN/OGRN/issuer identifiers;
- public website/contact information;
- public registered/postal/other disclosed addresses;
- issuer status;
- registrar information;
- accounting/interim/consolidated reports;
- annual reports;
- affiliated-person lists;
- charters and corporate documents;
- significant/corporate messages;
- corporate actions and changes;
- disclosed bank details;
- other facts later approved for Company Card, Monitoring or Risk.

## Publicly disclosed bank details

Store separately from inferred/unknown bank accounts.

Recommended fields:

- account number;
- bank name;
- BIC;
- correspondent account;
- recipient INN/KPP if disclosed;
- stated purpose of the account if present;
- source document/page;
- publication/retrieval date;
- currentness confidence.

Product wording:

`Публично раскрытые банковские реквизиты`

Do not automatically label a disclosed account as:

- the only bank account;
- the primary operating account;
- a currently active account;
- a blocked account.

Those require separate evidence.

## Relationship to FNS account-suspension checks

Corporate disclosure may reveal a bank/BIC/account.

FNS account-suspension data is a separate source/check.

If the FNS public service proves an active suspension decision for a bank/BIC, the product may connect those facts at bank level. Do not claim that a particular disclosed account number is blocked unless the source explicitly proves it.

## Relationship to Company Card v2

Corporate disclosure enriches:

- contacts;
- addresses;
- bank details;
- corporate history;
- related/affiliated persons and entities;
- financial documents;
- monitoring events.

Absence of disclosure must not automatically create a negative signal for a company that is not required to disclose this information.

## Stage 1.5 acceptance

Before Stage 1.5 closes:

- at least one free public disclosure adapter must work end-to-end for real companies;
- exact company matching must be confirmed;
- extracted facts must retain source URL and retrieval/publication date;
- publicly disclosed bank details must be distinguishable from inferred/current bank accounts;
- browser verification must confirm visible product data;
- fallback/public-source strategy must be documented;
- access must remain low-load and cache-first.

Paid API/FTP feeds remain a later optional business decision and are not required for Stage 1.5.

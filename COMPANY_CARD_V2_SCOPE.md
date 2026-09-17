# COMPANY CARD V2 SCOPE — Kontragent

Status: APPROVED PRODUCT SCOPE
Date: 2026-09-17

## Principle

Company Card v2 is the final product presentation layer.

Correct order:

`data -> normalisation -> applicability -> derived metrics -> Risk Engine -> Summary Engine -> Company Card v2 -> Report v1`

The card must not invent risk itself. It presents verified facts, calculations, conclusions, uncertainty and sources.

## 1. Core company identity

- full and short name;
- INN;
- KPP;
- OGRN;
- OKPO where available;
- legal form;
- registration date;
- current status;
- charter capital;
- main and additional OKVED;
- branches/representative offices where available;
- registration history where reliable.

## 2. Contacts

- website(s);
- phone(s);
- email(s);
- source for each contact;
- retrieval/publication date;
- currentness/confidence where the source can become stale.

Do not present an old or third-party contact as definitely current without provenance.

## 3. Addresses

Keep separate fields for:

- registered/legal address;
- factual address only when independently supported;
- postal address where available.

Each address keeps source and date.

### Mass address logic

Do not use a simplistic “many companies in one building = bad” rule.

Keep at least:

- number of active companies at exact normalised address;
- number at building level;
- room/office detail when available;
- official unreliability/address flags if present;
- uncertainty/explanation for shopping centres, business centres, technoparks and other multi-tenant buildings.

Product wording must explain context instead of applying an automatic fraud label.

## 4. Management and ownership

- current director/management;
- founders/owners;
- ownership shares where reliable;
- historical management/ownership where available;
- linked companies;
- relationship reason;
- relationship start/end dates;
- current/historical state;
- source/evidence.

### Mass director / founder

If identity is reliably matched, show concrete counts and linked organisations.

Example:

`Текущий руководитель также руководит 8 действующими юридическими лицами и является учредителем 5.`

A mass director/founder fact is a context signal, not automatic proof of misconduct. FIO-only matching is not enough for a strong negative conclusion.

## 5. Financials

At minimum with currently available datasets:

- revenue;
- expenses;
- profit/loss when available;
- employee count;
- year/period;
- trend.

After sufficient accounting data is available, add explainable ratios, for example:

- current liquidity;
- quick liquidity;
- absolute liquidity;
- equity/assets;
- liabilities/assets;
- debt/revenue;
- profitability;
- working capital;
- other approved financial-distress inputs.

Every material ratio must be expandable to show the values and formula used.

## 6. Taxes

The card should not be poorer than the verified FNS company-check blocks available to the project.

Separate facts/sections include where supported:

- tax debt;
- taxes/fees paid by type and period;
- penalties;
- fines;
- tax offences;
- special tax regimes;
- failure to submit tax reporting for the applicable period where available;
- debt above the applicable published threshold sent for enforcement to a bailiff where that specific dataset is available;
- employee count;
- income/expenses from accounting source;
- dates of each dataset.

Source limitations/exclusions must remain visible.

## 7. Bank details and account restrictions

### Publicly disclosed bank details

Show separately:

- account number;
- bank;
- BIC;
- correspondent account;
- stated purpose if disclosed;
- source;
- publication/retrieval date.

Do not call any disclosed account “the main account” unless the source proves that meaning.

### FNS account suspension decisions

Separate block:

`Операции по банковским счетам`

Show where available:

- active suspension fact;
- number of active decisions;
- decision number/date;
- tax authority;
- reason;
- BIC;
- check date.

Do not claim a specific bank account number is blocked without direct source evidence.

## 8. Regulatory data

Separate checks, not one generic regulator flag:

- CBR financial-organisation/licence status;
- CBR Warning List / illegal-activity signs;
- Bank of Russia KYC/high-risk public check when successfully available;
- Roszdravnadzor;
- Roskomnadzor;
- SRO / NOSTROY / NOPRIZ;
- ERKNM control events;
- other sector registries activated by applicability.

## 9. Courts

Minimum Court v1 presentation:

- number of cases;
- role in cases;
- active cases;
- active claim amount;
- largest claims;
- stages/instances;
- confirmed awarded amounts where known;
- claims/revenue ratio when meaningful;
- links/evidence;
- source/date.

Court claims are not confirmed debt.

Deep document analysis and outcome prediction are future Court Intelligence layers.

## 10. Enforcement

Where FSSP implementation is verified/recovered:

- active enforcement proceedings count;
- total active amount;
- categories where reliable;
- latest events;
- enforcement/revenue ratio when revenue exists;
- source/date.

If revenue is unavailable, show the amount but do not invent relative materiality.

## 11. Bankruptcy and liquidation

Show an event timeline, not a single boolean.

Possible facts include:

- intent to file;
- application filed;
- court acceptance;
- introduced bankruptcy procedure/stage;
- procedure changes;
- procedure completed/terminated;
- liquidation decision;
- liquidation process;
- planned exclusion/exclusion where applicable.

Liquidation and bankruptcy must remain distinct unless evidence connects them.

## 12. Corporate disclosure

When applicable:

- latest corporate/significant messages;
- financial/interim reports;
- affiliated-person lists;
- registrar;
- corporate documents;
- public bank details;
- public contacts/addresses;
- monitoring-relevant events.

Absence of disclosure is not automatically negative where the company has no such disclosure duty.

## 13. Relationships and environment

Every relationship should answer:

`who -> to whom -> relationship type -> why linked -> from/to dates -> current/history -> source`

Environment summary for each linked company may show concise status and key warnings, but must not obscure the reason for the relationship.

## 14. Risk and Summary

Company Card v2 consumes Risk Engine and Summary Engine output.

Quantitative wording rule:

- no “large debt”, “many claims”, “rapid growth” without numbers, period and calculation where calculable;
- show concrete numerator/denominator;
- separate fact from interpretation;
- preserve forecast vs confirmed fact.

## 15. Полнота проверки

User-facing Russian term: `Полнота проверки`.

This is not company risk.

It answers:

`Какие применимые проверки мы реально смогли выполнить?`

Show separately:

- applicable checks;
- completed checks;
- unavailable checks;
- not-applicable checks;
- explicit warning when important sources could not be checked.

Never convert source failure into a clean result.

## 16. Report v1

The card must support an auditable report containing:

- snapshot date/time;
- company identity;
- facts;
- calculations;
- risk/summary;
- missing/unavailable checks;
- source list;
- ruleset/engine version.

## 17. Final acceptance

Company Card v2 is not accepted merely because HTML renders.

Acceptance requires the separate `PRODUCT_CARD_ACCEPTANCE_GATE.md`: search, navigation, clicks, auth, source-data correctness, calculations, Risk, Summary, browser behaviour and real-company golden cases.

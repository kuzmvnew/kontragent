# SUMMARY ENGINE SPEC — Kontragent

Status: APPROVED DESIGN
Date: 2026-09-17

## 1. Purpose

Summary Engine explains already calculated, evidence-backed conclusions in human language. It does not independently decide risk from raw source data.

Pipeline position:

`Evidence -> Facts -> Derived Metrics -> Risk Rules -> Section Assessment -> Summary`

LLM/AI may help phrase text later, but factual content must come from structured assessments, calculations and source references.

## 2. Required inputs

Summary may use:

- section assessments;
- facts;
- derived metrics;
- signals;
- recommendations;
- coverage;
- data dates;
- change events;
- DealContext;
- risk/ruleset version.

Summary must not invent a missing amount, date, source, cause or legal meaning.

## 3. Required structure

A full user summary should be able to contain five blocks:

1. Short conclusion.
2. Main factors — usually up to 3–5.
3. What changed.
4. What could not be checked / insufficient data.
5. Recommended next action.

## 4. Numeric-first wording

Any quantitative interpretation must expose the underlying values when available.

Bad:

“Significant enforcement debt was found.”

Good:

“Active enforcement proceedings total 8.0m RUB. Revenue for 2025 is 20.0m RUB. The amount equals 40% of annual revenue.”

Bad:

“Court exposure is high.”

Good:

“Open arbitration claims against the company total 120m RUB across 7 active defendant cases. Revenue for 2025 is 80m RUB, so the claimed amount equals 150% of annual revenue. Claimed amounts are not the same as confirmed debt.”

Bad:

“Tax debt is growing fast.”

Good:

“Tax debt increased from 4.2m to 11.8m RUB over 6 months: +7.6m RUB, or +181%.”

## 5. Missing denominators

If a relative calculation cannot be made, Summary says so directly.

Example:

“Active enforcement proceedings total 8.0m RUB. Financial data for the latest complete reporting period are unavailable, so the amount cannot be assessed relative to annual revenue.”

Do not call the amount “large” or “small” without a valid comparison or explicit rule that does not require a denominator.

## 6. Court wording

Always distinguish:

- claimed amount;
- awarded amount;
- confirmed/entered-into-force obligation where known;
- current procedural stage;
- prediction.

Never convert an open claim into confirmed debt.

## 7. Bankruptcy wording

Separate:

- factual bankruptcy/procedure status from official sources;
- financial distress forecast.

A forecast must never be worded as an existing bankruptcy fact.

## 8. Coverage wording

Summary must report incompleteness where material.

Example:

“Court data were unavailable at the time of the check. Absence of court risk has therefore not been confirmed.”

Never use unavailable source data to produce “nothing was found”.

## 9. Modes

### Public Summary

For public/SEO company cards. Uses only fields and conclusions that are legally approved for public display.

### User Summary

For an authenticated user’s ordinary company check.

### Due Diligence Summary

For PDF/report. More formal language; includes sources, data dates, methodology version and limitations.

### Monitoring Summary

Explains what changed since the prior accepted state.

Example:

“Active enforcement amount rose from 2.0m to 8.0m RUB (+6.0m). With 2025 revenue of 20m RUB, the ratio increased from 10% to 40%.”

### Bulk Summary

Compact one-line/column output suitable for lists, for example:

“2 warnings; 0 critical; Core checks 9/10; one source unavailable.”

### Person Summary

Separate presentation/privacy rules. No public overexposure of personal data.

## 10. Explainability payload

Every important generated statement should be traceable to a structured payload such as:

- statement id;
- fact ids;
- metric ids;
- rule id/version;
- source/evidence ids;
- data date;
- calculation;
- summary engine version.

The UI may expose this through “why”, “calculation”, “source” or similar drill-down.

## 11. Versioning

Store at least:

- `summary_engine_version`;
- `risk_engine_version`;
- `ruleset_version`;
- generated timestamp.

If wording methodology changes without a source change, Monitoring must not present that as an external company event.

## 12. Prohibited behaviour

Summary must not:

- invent missing facts;
- hide source unavailability;
- use vague materiality language without calculations where calculations are required;
- confuse claim with debt;
- confuse forecast with fact;
- imply that `not_applicable` is positive;
- use person-related weak matches as confirmed negative conclusions;
- cite a source date as if it were the date of the underlying event when they differ.

# RISK ENGINE SPEC — Kontragent

Status: APPROVED DESIGN
Date: 2026-09-17

## 1. Purpose

Risk Engine converts verified source evidence into explainable section-level conclusions. It must remain extensible when new sources are added.

Core pipeline:

`Source -> Evidence -> Fact -> Applicability -> Derived Metrics -> Risk Rules -> Signal -> Section Assessment -> Coverage`

Risk Engine does not read raw web pages directly and does not let an LLM decide risk from unstructured source dumps.

## 2. Applicability comes before risk

The engine first decides which checks are relevant for this subject.

Applicability may depend on:

- entity type: legal entity / sole proprietor / person;
- legal form;
- company age;
- OKVED / actual business activity;
- regulated activity;
- participation in procurement;
- required licence or SRO;
- financial reporting duty;
- special status, for example financial organisation;
- DealContext: counterparty role, deal subject, territory, amount, advance, dates.

A check that is genuinely irrelevant is `not_applicable`; it does not reduce coverage and does not create a positive or negative signal.

Examples:

- construction SRO for an ordinary retailer: not applicable;
- construction SRO for a contractor in a construction deal: applicable;
- specialised taxi registry for a non-taxi company: not applicable.

## 3. Core and Context checks

### Core

Checks expected for most companies when the underlying data is legally and technically available:

- registration/status;
- management/ownership basics;
- tax facts;
- financial profile when reporting is expected;
- bankruptcy facts;
- enforcement facts;
- major regulatory restrictions.

### Context

Activated only when relevant:

- procurement;
- specific licences;
- SRO;
- transport registries;
- financial-market registries;
- healthcare/medical licences;
- sanctions/compliance;
- deal-specific requirements.

Coverage must be reported separately for Core and applicable Context checks where useful.

## 4. Check states

Use four base source-check states:

- `found` — source was successfully checked and a relevant record exists;
- `not_found` — source was successfully checked and no record exists within known coverage;
- `not_applicable` — this check should not apply to the subject/context;
- `unavailable` — the check should apply, but the source or data could not be reliably checked.

Never convert `unavailable` into `not_found`.

## 5. Risk and Coverage are separate

Known risk must never be reduced just because another source is unavailable.

Example:

- active bankruptcy is confirmed;
- court source is temporarily unavailable.

The bankruptcy signal stays critical/strong according to its own rule. Coverage is lower because the court section is incomplete.

Do not calculate `risk_score * coverage` as the main product result.

## 6. Facts are neutral

Facts describe what is known and must reference Evidence.

Examples:

- `enforcement.active_amount = 8_000_000 RUB`;
- `revenue.last_full_year = 20_000_000 RUB`;
- `court.active_defendant_claims = 120_000_000 RUB`;
- `company_age_months = 4`;
- `licence.status = suspended`.

A Fact is not positive or negative by itself.

## 7. Derived Metrics

Quantitative conclusions should be based on explicit calculations when denominators are valid.

Examples:

`enforcement_to_revenue = active_enforcement_amount / revenue_last_full_year`

`court_claims_to_revenue = active_defendant_claim_amount / revenue_last_full_year`

`tax_debt_to_revenue = tax_debt / revenue_last_full_year`

Other possible denominators:

- total assets;
- equity;
- cash/short-term assets;
- deal amount;
- industry benchmark.

Never silently substitute one denominator for another. If revenue is missing, do not pretend that `debt/revenue` was calculated from assets.

## 8. Missing data

If a rule requires data that is not available, that rule is not evaluated.

Example:

- enforcement amount = 8m RUB;
- revenue unavailable.

Valid result:

- `ENFORCEMENT_EXISTS = true`;
- `ENFORCEMENT_MATERIALITY = unknown/not_evaluated`.

Summary must say that 8m RUB was found but relative materiality cannot be calculated because the relevant financial denominator is unavailable.

For a newly registered company, annual-reporting rules may be `not_applicable_yet` at the domain level instead of ordinary source failure.

## 9. Quantitative wording rule

Do not write:

- “large debt”;
- “significant court exposure”;
- “many cases”;
- “rapid growth”.

unless the statement is backed by values, period and calculation.

Example:

- active enforcement: 8.0m RUB;
- revenue 2025: 20.0m RUB;
- ratio: 40%.

Then the signal may explain that the amount is material relative to annual revenue under the current ruleset.

If enforcement is 8m RUB and revenue is 8bn RUB, the ratio is 0.10%, and Summary must show those exact values rather than using vague wording.

## 10. Court materiality

Court rules should distinguish at least:

- active defendant cases count;
- active claim amount;
- claim amount / revenue;
- largest claim / revenue;
- new cases over period;
- growth in case count;
- amount actually awarded/lost where known;
- plaintiff vs defendant role;
- current procedural stage.

A claim is not a confirmed debt. Rules and Summary must preserve that distinction.

Example:

120m RUB active claims / 80m RUB annual revenue = 150%.

This can be described as material relative to business scale, while explicitly stating that the 120m RUB is claimed, not necessarily payable.

## 11. Rule structure

Each rule should be versioned and configurable.

Recommended fields:

- `rule_id`;
- `rule_version`;
- `section_code`;
- `entity_type/profile`;
- `applicability_conditions`;
- `required_facts`;
- `calculation`;
- `thresholds`;
- `severity`;
- `hard_blocker`;
- `freshness_policy`;
- `materiality_policy`;
- `deal_context_policy`;
- `explanation_template`;
- `recommendation`;
- `notification_policy`;
- `public_visibility`;
- `valid_from` / `valid_to`.

Thresholds must be versioned and later calibrated on real data. Do not hard-code arbitrary permanent thresholds in Python when they can live in a ruleset.

## 12. Hard rules

Some facts should not be diluted by unrelated positive metrics.

Examples subject to final legal/product calibration:

- active bankruptcy procedure;
- confirmed current blocking regulatory prohibition;
- missing mandatory licence for the exact regulated deal/activity;
- other legally decisive restrictions.

Hard rules require explicit evidence and applicability. They must not be inferred from weak identity matches.

## 13. Company profiles

Different profiles may have different rulesets while sharing the same engine:

- `GENERAL_LE`;
- `IP`;
- `NEW_COMPANY`;
- `FINANCIAL_ORG`;
- `NON_PROFIT`;
- later specialised regulated sectors.

The profile resolver changes applicable rules, not the underlying Evidence/Fact model.

## 14. Section model

Stable semantic sections should be based on user meaning, not source names:

- Registration;
- Ownership & Management;
- Finance;
- Taxes;
- Enforcement;
- Bankruptcy;
- Litigation;
- Procurement;
- Licences & Regulatory;
- Compliance.

Additional sector/context sections may be added where useful, but a new registry should not automatically create a new top-level risk section.

## 15. Output

Preferred product output:

- overall status: e.g. no material signals / attention / critical / insufficient data;
- Core coverage;
- Context coverage;
- critical signal count;
- warning signal count;
- unknown/incomplete sections;
- section assessments;
- recommendations;
- source/date/calculation drill-down.

A single public 0–100 “reliability score” is not required for v1. An internal technical score may later be used for ranking/filtering if separately designed and validated.

## 16. Versioning and recalculation

Every saved assessment should include:

- `assessment_id`;
- data snapshot or reproducible source references;
- `risk_engine_version`;
- `ruleset_version`;
- `ruleset_hash`;
- generated timestamp.

When rules change, old assessments remain historically reproducible. A change caused by a new ruleset must not be presented as if the company itself changed.

Change origins must distinguish at least:

- `SOURCE_CHANGE`;
- `RULESET_CHANGE`;
- `COVERAGE_CHANGE`;
- `DEAL_CONTEXT_CHANGE`.

# Risk Engine Rules v1

Date: 2026-09-17  
Ruleset: `risk-rules-1.0.0`  
Artifact: `app/risk_rules/v1.json`

## Rule contract

Every configured rule contains `rule_id`, `rule_version`, `section_code`, `profile`, `applicability_conditions`, `required_facts`, `calculation`, `thresholds`, `severity`, `hard_blocker`, `freshness_policy`, `materiality_policy`, `deal_context_policy`, `explanation_template`, `recommendation`, `public_visibility`, `valid_from` and `valid_to`.

The loaded JSON bytes are SHA-256 hashed and the hash is saved with every assessment. A later ruleset creates a new assessment; it cannot rewrite an old result.

## v1 rule families

| Rule | Meaning | Important boundary |
|---|---|---|
| `REGISTRATION_STATUS` | Interpret a present master-registry status | Missing status is informational/uncertain, not an invented adverse status |
| `TAX_DEBT_PRESENT` | Show amount and materiality when a valid revenue denominator exists | Thresholds are ruleset data; absence is only dated dataset absence |
| `TAX_OFFENCE_PRESENT` | Report published document/date/fine | The unpublished offence type is never invented |
| `ACTIVE_BANKRUPTCY_PROCEDURE` | Critical hard signal for evidence-backed active legal procedure | Liquidation, intent, application and completed procedure remain distinct |
| `COURT_ACTIVITY` | Report role/count/claims/period/coverage | A claim is not confirmed debt; partial sample is explicitly partial |
| `PROTECTED_REGULATORY_CHECK` | Interpret saved protected/public and coverage checks | Only a completed saved result can be negative; challenge/unavailable/stale is not clean |

## Quantitative tax rule

The initial configurable thresholds are:

- absolute warning amount: `1,000,000 RUB`;
- debt/revenue warning ratio: `0.10`.

These values are v1 product rules, not permanent universal truths. A debt signal always shows the actual amount and source date. The ratio is shown only when the saved revenue is a valid positive denominator. No assets or other value is silently substituted for revenue.

## Courts

Arbitration keeps `reported_total_cases`, `loaded_case_count`, `coverage_complete`, role counts, total/largest claims and 30/90/365-day metrics from the existing court contract. Partial loaded samples produce `PARTIAL_COVERAGE`. General courts remain targeted regional coverage and cannot produce a nationwide clean conclusion.

## Protected checks and regulatory semantics

- FNS account suspension: only a saved completed found result creates a strong signal; an old dataset becomes `STALE`; challenge/missing is not clean.
- CBR ZSK: found is a signal; a completed negative result means only absence in that public check/date, not absence from internal CBR scoring.
- CBR warning list: exact saved match creates a confirmed signal with the source caveat.
- Disqualified-person organisation link: never claims the person is the current director without a reliable relationship identity.
- Mass address: informational context with actual count and rule version; no fraud label.

## Explicit non-rules

- no public or internal 0–100 score in v1;
- no `risk * coverage` multiplication;
- no risk interpretation from raw web content;
- no FSSP/Fedresurs clean result while the source is not connected;
- no clean Roskomnadzor B/C result while `SOURCE_BLOCKED`;
- no forecast probability or bankruptcy forecast in this stage.


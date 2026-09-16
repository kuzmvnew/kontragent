# COURT INTELLIGENCE SPEC — Kontragent

Status: APPROVED DESIGN
Date: 2026-09-17

## 1. Goal

The court block must evolve from a simple list of cases into explainable legal-risk intelligence without confusing claims, judgments and forecasts.

## 2. Level 1 — Court Facts

At minimum store and analyse:

- case number;
- court/system;
- role of the company;
- parties;
- filing/open date;
- status/stage;
- instance;
- claim category;
- active claim amount;
- awarded amount where known;
- dates of key procedural events;
- current result where known;
- links/source identifiers;
- data/source dates.

Company-level metrics should include:

- active defendant cases count;
- active plaintiff cases count;
- active defendant claim amount;
- largest active claim;
- new cases over 3/6/12 months;
- case-count trend;
- amounts awarded/lost where legally meaningful;
- active claims / annual revenue;
- largest claim / annual revenue;
- other transparent materiality metrics.

## 3. Required wording distinction

A filed claim is not a confirmed debt.

The product must distinguish:

- claimed amount;
- court-awarded amount;
- amount under an act that has entered into force where this status is available;
- amount actually enforced/paid, if separately sourced;
- forecast exposure.

Example:

“Open defendant claims total 120m RUB across 7 cases. Revenue for 2025 is 80m RUB; claimed exposure equals 150% of annual revenue. The 120m RUB is the amount claimed, not confirmed debt.”

## 4. Level 2 — Analyse inside the case

The future data model must retain enough information to analyse actual court documents rather than only case metadata.

Recommended structure:

`Case -> Instance -> Hearing -> Document -> Claim -> Party -> Argument -> Evidence -> LegalRule -> Outcome`

From acts/documents, extract where lawfully accessible:

- subject of dispute;
- factual circumstances;
- original and amended claims;
- principal, penalty, interest and other components;
- claimant arguments;
- defendant arguments;
- evidence relied upon;
- expert examinations;
- interim measures;
- legal provisions applied;
- first-instance outcome;
- appeal outcome;
- cassation outcome;
- reversal/modification;
- final legally effective outcome where determinable.

The ingestion design should preserve source document identifiers, metadata and text/storage policy so that future analytics does not require rebuilding the entire historical corpus from scratch.

## 5. Level 3 — Future outcome prediction

Prediction is a future analytical feature, not an MVP requirement and not a substitute for actual court facts.

Potential outputs may include probabilities for:

- full satisfaction;
- partial satisfaction;
- rejection;
- procedural/non-merits outcome.

Possible features may include:

- case category;
- procedural stage;
- claim type;
- legal provisions;
- first-instance result;
- analogous historical cases;
- amounts;
- court/instance;
- party behaviour/events;
- document-derived features.

Any model must be validated on historical out-of-time data and return confidence/limitations.

## 6. Expected financial exposure

A future analytics layer may calculate expected exposure, for example from predicted satisfaction share, but it must be labelled as a model estimate.

`expected_exposure = claim_amount * estimated_satisfaction_share`

Do not turn predicted exposure into hard debt or a hard blocker.

## 7. Access gate

Before implementation of mass judicial ingestion, confirm:

- official or licensed machine-readable access path;
- automation terms;
- storage rights;
- document-text reuse rights;
- commercial/public display rights;
- rate limits;
- cost;
- personal-data restrictions.

Do not assume that public browser access automatically permits unlimited commercial bulk copying.

## 8. Risk Engine integration

Court Intelligence publishes Facts and Derived Metrics. Risk Engine applies versioned rules.

New court analytics must not hard-code prose directly into source adapters.

Examples of facts:

- `court.active_defendant_cases_count`;
- `court.active_defendant_claim_amount`;
- `court.claims_to_revenue_ratio`;
- `court.largest_claim_to_revenue_ratio`;
- `court.new_cases_12m`;
- `court.awarded_against_amount`.

Summary then explains those values with exact numbers, periods and legal caveats.

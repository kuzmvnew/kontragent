# Summary Engine Wording Rules v1

Date: 2026-09-17  
Engine: `summary-engine-1.0.1`

## General

- Explain persisted Risk Engine output; never infer risk from raw source material.
- Do not call a company reliable/unreliable or automatically recommend refusal to transact.
- Risk and completeness are separate. Low observed risk with incomplete checks is not a safety claim.
- Show optional blocks only when statements exist.

## Quantities

- Show value, unit, period/date, denominator, ratio and calculation when present in the persisted signal.
- If the denominator is absent, state that relative materiality was not calculated.
- Do not use “large”, “many”, “significant” or “rapid” without an applicable versioned rule and supporting values.

## Courts and bankruptcy

- Use “сумма заявленных требований”; a claim is not confirmed debt.
- Distinguish claimed, awarded and entered-into-force amounts only when the saved facts do.
- Show partial/targeted court coverage and do not claim nationwide absence.
- Keep official bankruptcy procedure separate from liquidation and from any future financial-distress forecast.

## Protected and unavailable checks

- Negative ZSK wording is limited to absence of high-risk information in the public Bank of Russia check on the stated date; it is not a reliability endorsement.
- Fresh account suspension results show saved fact/date/source. Stale or challenged results say that an up-to-date check is not confirmed.
- `unavailable`, `not_checked`, `source_blocked`, `access_pending`, `stale` and `partial` never become “nothing found”.
- `not_applicable` is neither positive nor negative and is normally omitted from the short summary while remaining in the Risk payload.

## Recommendations

Recommendations remain tied to signals and context: request settlement evidence, inspect case stages/acts, repeat an unavailable source check, clarify a procedure, or verify a deal-specific permission. The engine does not emit an unconditional “do not work with the company” recommendation.

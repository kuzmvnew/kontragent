# DEV-012 — S02 FNS Tax Debt first vertical slice

## Boundary

DEV-012 connects the already persisted S02 publication to the existing Risk
v3 and Summary v3 paths:

`S02 persisted state -> S02 fact contract -> Risk v3 -> Summary v3 -> public/card data`

The slice is DB-only. It does not fetch a source, invoke a parser or worker,
change source contracts, add an API route, render a frontend, or change Risk
or Summary rules.

## Fact contract

`S02TaxDebtFact` keeps these axes separate:

- state: `FOUND`, `NOT_FOUND`, `STALE_DATA`, `SOURCE_UNAVAILABLE`;
- amount and arrears/penalties/fines breakdown;
- amount/data date;
- canonical `S02` / `fns_tax_debt` source attribution;
- freshness dates, official validity boundary and reason;
- internal RAW/parser/normalization/matching provenance;
- evidence references and source limitations.

`FOUND` requires a current dated amount and an exact arithmetic breakdown.
`NOT_FOUND` requires a current dated snapshot and proven negative closure.
Stale or unavailable input cannot carry an amount or a debt signal.

## Risk and Summary

The persisted-evidence boundary now creates the S02 fact first and adapts that
fact to the existing `NormalizedEvidenceCandidate` contract. The Risk Engine
and its ruleset are unchanged. A positive current amount is still evaluated
by the existing `TAX_DEBT_PRESENT` rule. A fresh absence or zero amount becomes
a completed `NOT_FOUND`; stale and unavailable states remain unresolved and
block a clean positive conclusion.

The existing Summary v3 projection receives the Risk result unchanged. For a
confirmed debt it retains the S02 fact code, amount, date and source id in the
structured confirmed-positive-fact trace.

## Public projection and card data

`S02CardData` prepares the next frontend stage with four explicit blocks:

- Summary: conclusion, reason references, confirmed fact codes and actions;
- Risk: categorical result and S02 factors (no score);
- Evidence/Freshness: amount, date, official source and state;
- Coverage/Limitations: resolved state, negative closure, X/Y coverage and
  source limitations.

The projection excludes RAW file URIs, worker run ids, source member names,
record hashes and artifact checksums. Those values remain in the internal fact
provenance and Risk input snapshot.

## Entry points

- `load_s02_tax_debt_fact` reads persisted S02 rows and freshness state;
- `s02_tax_debt_fact_to_risk_candidate` performs the FACT -> Risk adaptation;
- `calculate_s02_vertical_slice` runs existing Risk and Summary over a bounded
  candidate snapshot;
- `calculate_s02_vertical_slice_from_persisted` is the DB-only application
  entry point;
- `build_s02_public_projection` creates `S02CardData` without changing the API.

## Limitations

- legal entities with valid 10-digit INN only;
- dated FNS publication, not a real-time balance;
- later repayments may not be reflected;
- the fact does not prove transfer to a bailiff;
- frontend/card rendering and API exposure are intentionally deferred;
- no new source and no mass-ingestion enablement are included.

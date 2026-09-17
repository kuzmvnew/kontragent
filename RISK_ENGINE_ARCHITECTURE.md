# Risk Engine Architecture

Date: 2026-09-17  
Status: implemented; acceptance status is recorded separately in `RISK_ENGINE_ACCEPTANCE.md`

## Boundary

Risk Engine v1 is an on-demand deterministic layer over existing normalized PostgreSQL facts and cached check contracts:

`Source -> Evidence/check result -> Fact/input snapshot -> Applicability -> Derived metric -> Versioned rule -> Signal -> Section assessment -> Coverage/completeness`

It does not fetch raw pages, invoke source providers, or ask an LLM to classify risk. Opening a company card only reads the latest saved assessment. The explicit `POST /company/{inn}/risk-assessment` action recalculates from saved inputs.

## Reused foundation

- `app/contracts/decision.py`: four base source states and Evidence/Coverage invariants;
- `app/contracts/assessment.py`: neutral Fact, traceable Signal and section contracts;
- `app/contracts/report.py`: DealContext;
- `DataSource`, `DataSet`, `IngestionRun`: source identity, operational status, dates, freshness and coverage;
- existing product aggregator: normalized/cached company checks only;
- `CompanyLegalEvent`: accepted 15-state bankruptcy/liquidation event semantics;
- arbitration/general-court cached checks and their explicit coverage fields;
- `CompanyPublicFact`: source-backed contextual company facts.

No second source registry or raw-page risk path was added.

## Public contracts

`app/contracts/risk.py` defines:

- profiles: `GENERAL_LE`, `IP`, `NEW_COMPANY`, `FINANCIAL_ORG`, `NON_PROFIT`;
- ten stable semantic categories;
- signal states: `CONFIRMED_RISK`, `WARNING`, `INFO`, `NO_RISK_FOUND`, `NOT_CHECKED`, `UNAVAILABLE`, `NOT_APPLICABLE`, `STALE`, `PARTIAL_COVERAGE`;
- severity and confidence, independently;
- applicability as `applicable`, `not_applicable`, or `unknown_unavailable`;
- separate Core/Context coverage and completeness counts;
- overall qualitative state without a 0–100 score;
- change origins: `SOURCE_CHANGE`, `RULESET_CHANGE`, `COVERAGE_CHANGE`, `DEAL_CONTEXT_CHANGE`.

Each signal keeps source/dataset, evidence refs, observed value, period, optional threshold/calculation, source/check/calculation timestamps, coverage/freshness and rule/version.

## Applicability

Profile is resolved before context rules. Sector/context checks use entity type, age, saved CBR participant result, legal-form text, OKVED/activity and DealContext. A check not activated by these inputs is `NOT_APPLICABLE`, does not count as applicable coverage and does not become a positive conclusion.

Procurement and licence/SRO checks are context checks. EIS RNP remains `ACCESS_PENDING`; an applicable procurement check is therefore `UNAVAILABLE`, never `NO_RISK_FOUND`.

## Risk and completeness

Known risk is selected independently of incomplete sources. A confirmed tax/CBR/bankruptcy signal remains unchanged when courts, FSSP, Fedresurs or Roskomnadzor are not available. Completeness separately counts applicable, completed, not-applicable, not-checked, unavailable, stale and partial checks, with Core and Context projections.

FSSP and Fedresurs produce explicit `NOT_CHECKED / source_not_connected` limitations because their accepted bounded inventories found no implementation. Roskomnadzor W1-005 B/C produces `UNAVAILABLE / source_blocked`.

## Persistence and reproducibility

Migration `e5f6a7b8c9d0` adds immutable `company_risk_assessments` rows. Each row stores:

- public UUID assessment id and company id;
- engine/ruleset versions and ruleset SHA-256;
- input and DealContext hashes;
- calculation timestamp and change origin;
- reproducible input snapshot and evidence refs;
- signals, sections, coverage, completeness, overall state and limitations;
- complete serialized result payload.

Identical engine/ruleset/input/context returns the existing assessment with `reused=true`. Changed facts, coverage/freshness, ruleset or DealContext invalidate reuse. Forced recalculation writes a new immutable row; prior assessments are not overwritten.

## Presentation

The existing company card contains a deliberately small acceptance surface:

- overall qualitative status and profile;
- section states/severity/confidence;
- completeness counts;
- top risk/limitation signals;
- expandable values, calculations, evidence source/dataset, dates, coverage/freshness and rule version;
- explicit calculate/recalculate action.

The former placeholder “Индекс надёжности” block was replaced. Company Card v2 and Summary Engine are not implemented by this stage.


# Summary Engine Architecture

Date: 2026-09-17  
Status: implemented; acceptance status is recorded separately in `SUMMARY_ENGINE_ACCEPTANCE.md`

## Boundary

Summary Engine v1 is a deterministic projection over a saved `RiskAssessmentResult`:

`persisted Risk Assessment -> ordered traceable statements -> mode projection -> immutable summary`

It does not fetch providers, parse raw HTML/XML/API payloads, or make new risk decisions. Risk status, severity, coverage, completeness, rules and evidence references remain owned by Risk Engine. No LLM or external AI API is used.

## Contracts and modes

`app/contracts/summary.py` defines `PUBLIC`, `USER`, `DUE_DILIGENCE`, `MONITORING`, `BULK` and `PERSON` modes, statement kinds, text blocks, monitoring comparison and the persisted result.

- `USER` is the fully presented v1 mode.
- `PUBLIC` is a filtering contract only. It applies rule visibility/privacy filtering and remains explicitly `public_projection_approved=false` until Legal Launch Gate.
- `DUE_DILIGENCE` retains sources, evidence refs, dates, calculation and methodology versions for future Report v1; no PDF is generated.
- `MONITORING` distinguishes `SOURCE_CHANGE`, `RULESET_CHANGE`, `COVERAGE_CHANGE` and `DEAL_CONTEXT_CHANGE`. Only source change is classified as a possible company change; ruleset-only wording is methodology, not a company event.
- `BULK` adds a compact line while retaining the structured payload.
- `PERSON` applies a privacy boundary and excludes `PRIVATE_INTERNAL` person evidence. Person Core is not implemented.

## Deterministic statements

The full user result can contain:

1. short conclusion;
2. main factors;
3. changes for Monitoring;
4. limitations;
5. recommended next actions.

Empty optional blocks are omitted by the template. Factor ordering is deterministic: hard blocker, severity, confidence, numeric materiality, recency, stable signal code. Every critical factor survives the normal factor limit.

Wording is selected by signal/rule code. Quantitative factors expose available values, periods, denominator, ratio, threshold/calculation and data date. Missing denominators are stated. Court claims stay claims, bankruptcy fact stays separate from forecast, and unavailable/not-checked/partial/stale states never become a clean result.

## Traceability

Every meaningful statement stores:

- statement, summary and risk-assessment ids;
- signal ids and fact/metric ids when available;
- rule id/version;
- source and evidence refs;
- data dates and calculation;
- severity/hard-blocker/public-visibility metadata;
- Summary Engine version.

The card’s “Почему” links lead to the existing Risk/evidence details. Summary does not duplicate raw source payloads.

## Persistence and cache

Migration `f6a7b8c9d0e1` adds immutable `company_summaries` rows with structured payload, text blocks and explainability refs. Cache identity is:

`risk_assessment_id + mode + summary_engine_version + deal_context_hash + projection_policy_version`

A new risk assessment, mode, Summary Engine wording version, DealContext hash or projection/legal-policy version invalidates reuse. Existing summaries are not overwritten when wording methodology changes.

## Presentation

The existing company card receives only a minimal acceptance block: short conclusion, main factors, limitations, actions, generation timestamp and versions. It does not implement Company Card v2. Opening a card reads saved Risk/Summary results and never triggers provider access. Summary generation is an explicit POST action over the latest saved Risk assessment.

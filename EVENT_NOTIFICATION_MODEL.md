# EVENT & NOTIFICATION MODEL — Kontragent

Status: APPROVED DESIGN
Date: 2026-09-17

## 1. Goal

Monitoring must be one extensible engine, not a separate monitoring implementation for every source.

Base contract:

`entity -> source -> old_value -> new_value -> event_type -> occurred_at -> detected_at -> evidence -> severity`

Recommended event object also stores:

- event id;
- entity id/type;
- section;
- source/dataset;
- source record ids;
- old/new structured values;
- occurred_at when source provides it;
- detected_at;
- source_as_of;
- event origin;
- dedupe key;
- evidence refs;
- rule/risk delta refs where applicable.

## 2. Event origin

At minimum distinguish:

- `SOURCE_CHANGE` — company/source facts changed;
- `RULESET_CHANGE` — methodology changed, company did not;
- `COVERAGE_CHANGE` — source became available/unavailable or coverage changed;
- `DEAL_CONTEXT_CHANGE` — user changed deal assumptions/context.

Only SOURCE_CHANGE is an external company event by default.

## 3. Event families

### Corporate

- company status changed;
- name changed;
- address changed;
- director changed;
- owner/founder changed;
- capital changed;
- branch/subsidiary changes.

### Tax / Finance

- new tax debt;
- tax debt amount changed;
- tax offence appeared/changed;
- new annual financial statements;
- material revenue/profit/equity change.

### Enforcement

- new enforcement proceeding;
- proceeding closed;
- enforcement amount increased/decreased;
- materiality ratio crossed a versioned threshold.

### Courts

- new case;
- role/claim changed;
- hearing/instance changed;
- judgment appeared;
- judgment changed/reversed;
- material claim exposure changed.

### Bankruptcy

- bankruptcy intention appeared;
- procedure started;
- stage changed;
- procedure completed/terminated;
- official bankruptcy record removed/expired where applicable.

### Regulatory

- licence issued;
- licence suspended;
- licence terminated;
- SRO membership changed;
- control/inspection event appeared;
- transport/regulatory status changed.

### Procurement

- RNP added/removed;
- procurement participation/contract events when supported by approved sources.

### Compliance

- sanctions/compliance record added/removed/changed;
- relevant ownership/relationship change affecting compliance.

### Positive / resolved

Closing/removal events are first-class events. Monitoring is not only negative alerts.

### Internal data-quality

- source update failed;
- source became unavailable;
- coverage dropped;
- source schema/version changed;
- import validation failed.

These are primarily operator/admin events, not ordinary client alerts.

## 4. New sources

A new source should normally require only:

1. source adapter/ingestion;
2. Facts mapping;
3. change detector/event mapping;
4. optional new risk rules;
5. optional new event types.

It must not require rewriting the core Monitoring or Notification Engine.

## 5. Risk delta

An event may trigger assessment recalculation.

Store separately:

- event itself;
- previous assessment;
- new assessment;
- risk/section changes;
- ruleset version.

Example:

Enforcement changed from 2m to 8m RUB; revenue remains 20m RUB.

Event:

`ENFORCEMENT_AMOUNT_CHANGED`

Derived change:

- ratio 10% -> 40%;
- corresponding signal severity may change under the active ruleset.

Notification may state both the raw amount delta and the recalculated ratio.

## 6. Notification policy

Notification Engine receives events and assessment deltas. It does not re-read sources independently.

Possible delivery policies:

- in-app feed only;
- immediate email;
- daily digest;
- weekly digest;
- muted;
- later webhook/API.

User/workspace settings may filter by:

- event family;
- company/list;
- severity;
- critical-only;
- positive/resolved events;
- delivery channel.

## 7. Dedupe and idempotency

Repeated ingestion of the same source state must not create repeated user events.

Each event type needs a deterministic dedupe key based on stable source/entity/event semantics.

Updates should be idempotent and preserve event history.

## 8. Summary integration

Monitoring Summary explains:

- what changed;
- old value;
- new value;
- absolute delta;
- relative delta when meaningful;
- impact on derived metrics/section status;
- source/date;
- limitations.

No vague wording when exact numbers exist.

## 9. Extensibility

Event type registry must be versionable. Adding dozens or hundreds of future notification types should be data/configuration work plus source mapping, not a rewrite of the engine.

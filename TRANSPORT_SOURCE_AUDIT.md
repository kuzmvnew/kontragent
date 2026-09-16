# TRANSPORT SOURCE AUDIT — Kontragent

Status: APPROVED RESEARCH BACKLOG
Date: 2026-09-17

## 1. Why this document exists

The previous roadmap mentioned only:

- Register of freight forwarders;
- Rostransnadzor registries.

That is too broad for implementation. Transport must be split into concrete context-dependent source blocks and source passports.

Nothing in this document bypasses `SOURCE_ACCESS_COST_GATE.md`. A registry being public on a website is not enough to approve bulk ingestion, storage or commercial redistribution.

## 2. Core principle

Transport checks are mostly Context checks.

Risk Engine first determines applicability from entity type, OKVED/actual activity, licence requirements and DealContext. A transport registry that is irrelevant to the company becomes `not_applicable`, not a missing negative signal.

## 3. Wave 2 / high-priority transport backlog

### T1. GosLog — register of freight forwarders

Use for companies performing freight-forwarding activity where the registry is legally relevant.

Target facts:

- registry presence;
- registration/status;
- dates;
- identifiers;
- source freshness.

Priority: HIGH / Wave 2 context.

### T2. Rostransnadzor — bus/passenger road transport licensing

Separate source passport from other transport licensing.

Target facts:

- licence/permission presence;
- status;
- dates;
- relevant vehicles where legally/publicly available;
- suspension/termination events.

Priority: HIGH / Wave 2 context.

### T3. Rostransnadzor — international road carriers

Target facts:

- admission/registry presence;
- status;
- validity/period where available.

Priority: HIGH / Wave 2 context.

### T4. FGOS/FGIS Taxi — carrier register

Target facts:

- taxi carrier presence/status;
- region;
- validity/current status;
- identifiers.

Priority: HIGH / Wave 2 context.

### T5. FGIS Taxi — ordering services

Separate registry/source contract from carriers.

Priority: Wave 2 context.

### T6. FGIS Taxi — taxi vehicles

Vehicle-level data must not be automatically published in company SEO without a separate public-field/privacy decision.

Priority: Wave 2 context.

## 4. Other transport context sources

### T7. Rostransnadzor — railway passenger transport licences

Context for railway operators.

### T8. Rostransnadzor — railway dangerous-goods / loading activities

Keep as separate source/passport where source structure or legal regime differs.

### T9. Rostransnadzor — sea/inland-water passenger and dangerous-goods licensing

Context for maritime/inland-water operators.

### T10. GIS EPD — operators of electronic transport-document information systems

Relevant only to companies operating such systems. Not a general logistics-company check.

### T11. Rosaviatsiya — aviation operators/certificates/registries

Potential sub-sources include, where relevant and lawfully machine-readable:

- operator certificates;
- commercial air-transport operators;
- aviation works operators;
- aerodrome/heliport registries and operators;
- training centres;
- aircraft-related registry data where company-level use is justified.

Aviation should not be treated as one generic source; split only after source/access research.

## 5. Future source

### T12. GosLog — road freight carrier register

Planned future mandatory road-freight carrier registry from 2027-03-01 according to current project research.

Status: FUTURE SOURCE / DO NOT IMPLEMENT YET.

Before activation date:

- monitor legal/technical launch;
- confirm exact official system;
- confirm machine-readable channel;
- confirm fields and identifiers;
- create source passport only when implementation becomes actionable.

## 6. Applicability examples

- ordinary retailer -> taxi/aviation/maritime checks not applicable;
- freight forwarder -> GosLog forwarder register applicable;
- taxi carrier -> FGIS Taxi carrier check applicable;
- international road carrier -> international admission register applicable;
- airline/aviation operator -> Rosaviatsiya context applicable;
- maritime passenger operator -> corresponding transport licence check applicable.

## 7. Risk Engine integration

Transport sources publish Facts such as:

- `transport.registry_presence`;
- `transport.permission_status`;
- `transport.licence_status`;
- `transport.valid_from` / `valid_to`;
- `transport.suspension_status`.

Risk rules then determine whether a missing/expired/suspended permission matters for the company/activity/deal.

Do not hard-code “not found in transport registry = risk” without proving that the registry is mandatory and applicable to the subject at the checked date.

## 8. Event types

Potential events:

- transport permission issued;
- transport permission suspended;
- transport permission terminated;
- registry entry added/removed;
- operator status changed;
- validity period changed.

These use the common `EVENT_NOTIFICATION_MODEL.md`.

## 9. Implementation rule

Before each transport source enters Wave 2 implementation:

1. confirm official owner/URL;
2. confirm identifiers/matching;
3. confirm machine-readable mode;
4. confirm automation/rate limits;
5. confirm storage/cache/publication/commercial-use rights;
6. record cost;
7. define refresh schedule;
8. define applicability;
9. define public/private fields;
10. pass the ordinary source acceptance protocol.

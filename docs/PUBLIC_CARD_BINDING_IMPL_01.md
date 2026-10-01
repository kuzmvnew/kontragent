# PUBLIC-CARD-BINDING-IMPL-01 — semantic company view

`Company.id` is the only internal company identity. INN is the exact public
locator. Public payloads never contain `company_id`.

## Contracts

- `CompanyResolutionV1` (`company-resolution-v1`) resolves an exact local INN
  to `RESOLVED`, `NOT_FOUND`, `AMBIGUOUS`, or `RESTRICTED`. It is read-only,
  performs no provider call, and never creates a `Company`.
- `SemanticFact` owns one stable semantic coordinate. `fact_ref` and
  `item_ref` are UUIDv5 values derived only from company, section, field,
  period, and semantic item identity. Value, source, parser path, database row
  id, and presentation order are not part of either anchor.
- `SemanticEvidence` retains selected and alternative evidence, per-fact source
  date, retrieval time, confidence, freshness, rights, and limitations.
- Persistence marks the current coordinate set and retains prior selected
  evidence in `evidence_history` when a value or source changes; superseded
  coordinates remain auditable instead of being deleted.
- `FinancePeriod` supports `YEAR`, `QUARTER`, and `DATE`.
- `FinanceMetric` keeps `REVENUE`, `EXPENSES`, `PROFIT_LOSS`, `NET_PROFIT`,
  `EQUITY`, `COMPANY_VALUE`, and `EMPLOYEE_COUNT` distinct.
- `CompanyViewModelV1` (`company-view-v1`) is the one semantic read model.

## Frontend binding

`GET /api/company/{inn}` keeps the compatibility fields and adds:

```json
{
  "view": {
    "contract_version": "company-view-v1",
    "revision": "cv1:<sha256>",
    "generated_at": "<timezone-aware timestamp>",
    "inn": "<10-digit INN>",
    "sections": [
      {
        "section_key": "finances",
        "title": "Финансы",
        "state": "<human-safe state>",
        "items": [
          {
            "fact_ref": "fact:<uuid>",
            "item_ref": "item:<uuid>",
            "field_key": "REVENUE",
            "label": "Выручка",
            "period": "YEAR:2025",
            "value": {"value": "9673000.00", "currency": "RUB"},
            "state": "Сведения найдены",
            "source": {
              "name": "Доходы и расходы по данным ФНС",
              "source_class": "Официальные открытые данные",
              "reference": null,
              "source_data_date": "2025-12-31",
              "retrieved_at": "<timestamp>",
              "confidence": 1.0,
              "freshness": "CURRENT"
            },
            "alternative_sources": [
              {
                "name": "Firmoteka · авторизованный вторичный источник",
                "source_class": "Авторизованный вторичный источник",
                "reference": "https://firmoteka.ru/{inn}",
                "source_data_date": "2025-12-31",
                "retrieved_at": "<timestamp>",
                "confidence": 0.75,
                "freshness": "CURRENT"
              }
            ],
            "limitations": []
          }
        ]
      }
    ],
    "action_context": null,
    "links": {
      "public_card": "/companies/{inn}",
      "public_api": "/api/company/{inn}"
    }
  }
}
```

The section order is stable:

`identity`, `status`, `registration`, `address`, `activity`, `management`,
`founders`, `contacts`, `capital`, `finances`, `employees`, `tax`, `enforcement`,
`licenses`, `courts`, `bankruptcy`, `procurement`, `restrictions`, `inspections`,
`connections`, `events`, `risk`, `summary`, `source_coverage`, `freshness`,
`limitations`, `anchors`, `links`.

SSR places the identical revision on the card root as
`data-view-revision`, exposes `data-section-key`, `data-fact-ref`, and
`data-item-ref` bindings, and renders the public semantic sections without
reading Firmoteka snapshots. Implemented bindings are data-driven; domains without
accepted evidence remain explicit `NOT_CHECKED`/limiting states rather than being
filled by provider-specific UI assumptions. The contract does not claim production
source acceptance or the pending pixel-perfect Card V2 design.

The ordinary public Risk payload is compiled from the persisted Risk model.
Internal `meaning_id`, factor/rule identifiers, and engine versions remain
available to internal processing but are omitted before the recursive
fail-closed public validator runs. Public API and visible SSR retain only the
compiled category, severity, explanations, user meaning, provenance, and
dates required for display.

## Rights and precedence

Selection is deterministic: official primary, official API/open data,
Firmoteka authorized bridge, then derived evidence. Ties use source data date,
retrieval time, and source code. Alternatives are retained. Materially
different values produce `CONFLICTING_EVIDENCE`.

Firmoteka authorized-bridge facts in the accepted company domains are
`PUBLIC`: legal identity/form/status/registration, address/region,
activities, manager name and position, founder name/share, capital, finance,
employee counts, tax history and paid taxes, licenses, safe divisions,
company events, and enforcement. Public selection still follows authority:
accepted official evidence wins and the bridge remains corroborating or
alternative evidence. When no official equivalent exists, the bridge fact is
the public selected evidence.

Rights are assigned by field/domain, never by hiding the whole source. A
related person is a typed value with a stable semantic `person_ref`, one or
more `MANAGER`, `FOUNDER`, `PARTICIPANT`, `INDIVIDUAL_ENTREPRENEUR`, or
`OTHER_PUBLIC_RELATION` relationships, `CURRENT`/`HISTORICAL` state,
related-company context, role/share, and dated provenance. Internal/authenticated
representations may retain person identifiers required for entity resolution, but
public company-card filtering removes personal INN/OGRNIP values. The same person can carry several relationships without creating
another `Company`.

Actually observed public-source phones and emails are typed facts in the
`contacts` section. Only contacts explicitly classified as `CORPORATE` are
eligible for the public company card. `PERSONAL` and `UNKNOWN` contacts are
`AUTHENTICATED_ONLY` and are filtered out of public HTML/API. Values still carry
`PHONE`/`EMAIL`, scope, optional related-person context, related company and
current/historical state for authorized product flows. No phone or email is
guessed, derived, or assigned to a person without explicit source context.

Passport fields, passport values, registration/residential/home addresses,
provider row IDs, raw payloads, checksums, parser fields, worker identifiers,
credentials, non-public source references, and internal source enums remain
forbidden by both normalization whitelists and recursive public validation.
The public provenance label is `Firmoteka · авторизованный вторичный источник`; the internal
source code is never serialized publicly.

## Firmoteka mapping

The bridge normalizer covers identity, legal form, registration/status,
address/region, OKVED and additional activities, management, founders/share,
authorized capital, multi-year finance, employee series, tax-debt history,
tax paid, licenses, divisions, events, and enforcement aggregate/items.
Finance, tax, employee, event, and enforcement dates are derived from their
own periods/snapshots. Registration date is never used as their freshness
date.

Collection values are explicit public whitelists rather than provider-shaped
objects. In particular, events retain only date and human description, while
enforcement rows use the case number, dates/state, subject, public amounts,
and department. No fixed UI limit is applied to event or enforcement rows.

The opt-in real E2E pins its evidence identity to ООО «АЛАН» snapshot
`1eac2a0e-c777-4173-bb71-6fd42cb0ca2d` and asserts debt periods
`DATE:2026-06-01`, `DATE:2026-07-01`, `DATE:2026-08-01`, and
`DATE:2026-09-01` as an exact set. A disposable database containing a
different retained snapshot is reported as unavailable for that assertion;
the test never fabricates or mutates evidence to satisfy the manifest.

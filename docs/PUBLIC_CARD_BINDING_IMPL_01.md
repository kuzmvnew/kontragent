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
        "state": "<human-safe state>",
        "items": [
          {
            "fact_ref": "fact:<uuid>",
            "item_ref": "item:<uuid>",
            "field_key": "REVENUE",
            "period": "YEAR:2025",
            "value": {"value": "9673000.00", "currency": "RUB"},
            "state": "Сведения найдены",
            "source": {
              "name": "Доходы и расходы по данным ФНС",
              "source_data_date": "2025-12-31",
              "retrieved_at": "<timestamp>",
              "confidence": 1.0,
              "freshness": "CURRENT"
            },
            "alternative_sources": [],
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
`founders`, `capital`, `finances`, `employees`, `tax`, `enforcement`,
`licenses`, `events`, `risk`, `summary`, `source_coverage`, `freshness`,
`limitations`, `anchors`, `links`.

SSR places the identical revision on the card root as
`data-view-revision` and exposes `data-section-key` bindings. Existing markup
remains version-compatible until the accepted Card V2 design is implemented.

## Rights and precedence

Selection is deterministic: official primary, official API/open data,
Firmoteka authorized bridge, then derived evidence. Ties use source data date,
retrieval time, and source code. Alternatives are retained. Materially
different values produce `CONFLICTING_EVIDENCE`.

Firmoteka-only facts default to `AUTHENTICATED_ONLY`. A public fact selected
from an accepted official source remains public, while its non-public bridge
alternative is removed from the public projection. Contacts are not
normalized unless an explicit accepted-rights flag is supplied.

## Firmoteka mapping

The bridge normalizer covers identity, legal form, registration/status,
address/region, OKVED and additional activities, management, founders/share,
authorized capital, multi-year finance, employee series, tax-debt history,
tax paid, licenses, divisions, events, and enforcement aggregate/items.
Finance, tax, employee, event, and enforcement dates are derived from their
own periods/snapshots. Registration date is never used as their freshness
date.

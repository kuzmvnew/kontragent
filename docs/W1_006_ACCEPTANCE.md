# W1-006 — НОСТРОЙ / НОПРИЗ / СРО: acceptance

Дата: 17.09.2026
Статус: **ACCEPTED / SIX GATES PASS на пользовательском Mac**

## Принятый scope

- company-safe exact-INN checks: НОСТРОЙ и НОПРИЗ;
- private person storage: НРС НОПРИЗ;
- НРС НОСТРОЙ: безопасное `unavailable/source_protection`, без обхода защиты;
- другие официальные профессиональные/SRO-реестры: отдельный backlog.

## Six Gates

| Gate | Статус | Evidence |
|---|---|---|
| 1 Tests | PASS | full regression: `472 passed` |
| 2 Official source | PASS | реальные official HTTP 200 NOSTROY, NOPRIZ и НРС НОПРИЗ; NOSTROY NRS protection честно зафиксирована |
| 3 PostgreSQL | PASS | Alembic `e7a8b9c0d1e2`; company cache и private person record независимо прочитаны |
| 4 State semantics | PASS | found/not_found/not_applicable/unavailable; timeout/protection не превращаются в absence |
| 5 Chromium | PASS | 3 реальные company cards, HTTP 200, `page_errors=[]`, visible exact-INN/date/source, person exposure false |
| 6 Counts/dates/coverage | PASS | on-demand coverage измерено отдельно от official reported counts; bulk completeness N/A |

Финальный acceptance runner завершился строкой:

```text
W1-006 SIX GATES: PASS
```

## PostgreSQL и coverage

- database: `kontragent`;
- migration head: `e7a8b9c0d1e2`;
- Master Registry: `6781487`;
- NOSTROY cache: `2`, success `2`, found `1`, not_found `1`;
- NOPRIZ cache: `2`, success `1`, error/timeout `1`, found `1`;
- unique checked company INN: `3`;
- exact Master matches: `3`;
- unchecked Master: `6781484`;
- private person records: `1`;
- public person records: `0`;
- linked/confirmed company relationships: `0`;
- manual-review person records: `1`.

Cache coverage не является bulk snapshot. Official reported counts на дату
исследования: NOSTROY members `419695`; NOPRIZ members `212903`; NRS NOPRIZ
`174395`; NRS NOSTROY page `321809`.

## Реальный Chromium

| INN | NOSTROY | NOPRIZ | HTTP/errors | Privacy |
|---|---|---|---|---|
| `5907056036` | found | unavailable/not_checked | `200`, `[]` | person fields absent |
| `9102309919` | not_found | unavailable/timeout | `200`, `[]` | person fields absent |
| `7810978295` | unavailable/not_checked | found | `200`, `[]` | person fields absent |

На всех карточках видны источник, объяснение exact-INN и дата проверки.
Acceptance PNG созданы локально для проверки и не коммитятся.

## Matching и privacy

Company match — только exact 10-digit INN с проверкой согласованности ответа;
OGRN и official record IDs — corroborating identifiers. Person match — только
official specialist ID/registration number или другой устойчивый официальный
идентификатор. ФИО-only match не подтверждается.

`sro_person_registry_records` — отдельный private/internal container с
evidence, dates, confidence, access classification и retention metadata.
Public company serializer/template/API не импортируют и не сериализуют person
model. Автотест и реальный browser check подтверждают нулевую экспозицию.

## Ограничения

- undocumented frontend endpoints: low-load on-demand, cache/backoff/budget;
- automation/storage/commercial reuse/republication: ACCESS / LEGAL REVIEW REQUIRED;
- NRS NOSTROY protection не обходится;
- auto-update: `NOT_CONFIGURED`;
- NOPRIZ timeout остаётся `unavailable`;
- W1-005 B/C остаются `IN PROGRESS / SOURCE-BLOCKED` и этим acceptance не закрываются.

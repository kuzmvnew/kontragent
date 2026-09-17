# W1-006 NOSTROY / NOPRIZ / SRO — final run report

Дата: 17.09.2026
Итог: **ACCEPTED / SIX GATES PASS на пользовательском Mac**

Первый изолированный проход был `IMPLEMENTED / INTEGRATION PENDING`. После
расширения разрешённого scope выполнена минимальная интеграция в существующие
models, aggregator, routes, template, tests и docs; новый PR не создавался.

## Реализовано

- NOSTROY company provider/cache: exact-INN on-demand;
- NOPRIZ company provider/cache: exact-INN on-demand;
- NOPRIZ NRS private person provider/storage;
- NOSTROY NRS adapter: `unavailable/source_protection`, без OCR/обхода;
- company-safe SRO projection и отдельный UI block;
- четыре dataset/source registrations;
- migration `e7a8b9c0d1e2`;
- full tests и acceptance runner.

## Evidence

- NOSTROY reported rows: `419695`; found `5907056036`; not_found `9102309919`;
- NOPRIZ reported rows: `212903`; found `7810978295`; request для
  `9102309919` timeout и остаётся unavailable;
- NRS NOPRIZ reported rows: `174395`; exact official registration lookup
  сохранён одной private записью;
- NRS NOSTROY page count: `321809`; protected image fields не извлекались.

PostgreSQL: Master `6781487`; NOSTROY cache `2`; NOPRIZ cache `2`; 3 unique
checked INN / 3 Master matches / `6781484` unchecked. Private person records
`1`; public person records `0`; confirmed/linked company relationships `0`;
manual review `1`. Bulk completeness N/A.

## Six Gates

1. Tests — PASS: `472 passed`.
2. Official source — PASS: реальные ответы официальных источников.
3. PostgreSQL — PASS: migration/write/readback.
4. Semantics — PASS: found/not_found/not_applicable/unavailable.
5. Chromium — PASS: three cards HTTP 200, `page_errors=[]`, private exposure false.
6. Counts/dates/coverage — PASS для on-demand модели; cache не выдан за bulk.

Acceptance runner: `W1-006 SIX GATES: PASS`.

## Ограничения

Undocumented APIs используются только low-load on-demand с cache/backoff.
Automation/storage/reuse/republication требуют Access/Legal review.
Auto-update `NOT_CONFIGURED`. Person evidence остаётся `PRIVATE_INTERNAL`;
ФИО-only matching запрещён. W1-005 не изменён: B/C остаются
`IN PROGRESS / SOURCE-BLOCKED`.

Подробности: `NOSTROY_SRO_SOURCE_PASSPORT.md` и
`docs/W1_006_ACCEPTANCE.md`.

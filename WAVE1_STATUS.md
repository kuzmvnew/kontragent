# Wave 1 — официальный промежуточный статус

Дата: 17.09.2026. Фаза 3 завершена в согласованном объёме; Фаза 4 ACTIVE.
Это статус результатов, не изменение утверждённой архитектуры.

| Шаг | Источник | Фактическое состояние |
|---|---|---|
| W1-001 | CBR Warning List | ACCEPTED / SIX GATES PASS на Mac |
| W1-002 | CBR FinOrg | ACCEPTED / SIX GATES PASS на Mac |
| W1-003 | ФНС — МСП, получатели поддержки | ACCEPTED / SIX GATES PASS на Mac |
| W1-004 | Росздравнадзор | ACCEPTED / SIX GATES PASS на Mac |
| W1-005 | Роскомнадзор | IN PROGRESS / SOURCE-BLOCKED (B, C) |
| W1-006 | НОСТРОЙ / СРО | NEXT EXECUTABLE / NOT STARTED |

Полностью приняты на пользовательском Mac 4 источника из 6. W1-005 Роскомнадзор реализован для scope A–F, но не принят: официальные bulk-файлы B/C обрываются до XML EOF и корректно остаются `unavailable`. W1-006 ещё не запускался и является следующим исполнимым шагом.

Разрешено начинать W1-006, пока W1-005 ожидает исправления/полного ответа официальных B/C-источников. Это не считается закрытием W1-005. Wave 1 остаётся открытой до приёмки W1-006 и отдельного закрытия/решения по W1-005 blocker.

## W1-001
Протокол: [docs/W1_001_ACCEPTANCE.md](docs/W1_001_ACCEPTANCE.md). Импорт не повторять.

## W1-002
Протокол: [docs/W1_002_ACCEPTANCE.md](docs/W1_002_ACCEPTANCE.md).
Локальный Mac acceptance 16.09.2026: `W1-002 SIX GATES: PASS`; gates 1—6 PASS; `accepted=true`; `user_mac_accepted=true`; PostgreSQL `kontragent`; Alembic `f7b2d8a4c1e3`; 435 tests PASS.
Реальный found: ИНН `9706063520`, БАНК ЭЛЕМЕНТ (ООО), CBR ID `2000000352020`, 2 лицензии/права, 2 active; HTTP 200; PostgreSQL reread `cached=true`; Chromium HTTP 200, `page_errors=[]`.
Реальный not_found: ИНН `9102309919`, АО «КРЫМАВТОДОР»; HTTP 200; PostgreSQL reread `cached=true`; Chromium HTTP 200, `page_errors=[]`.
Покрытие on-demand cache на дату проверки: Master Registry `6 781 485`; 3 успешных cache-INN, 2 из них относятся к Master Registry; 2 checked Master / 6 781 483 unchecked. Это НЕ bulk-реестр ЦБ. Дата — дата точечной проверки, не дата полного реестра. `auto_update=NOT_CONFIGURED`.

## W1-003
Финальная приёмка: [docs/W1_003_ACCEPTANCE.md](docs/W1_003_ACCEPTANCE.md). На Mac `SIX GATES PASS`; импорт не повторять.

## W1-004
Финальная приёмка: [docs/W1_004_ACCEPTANCE.md](docs/W1_004_ACCEPTANCE.md). Scope A–D принят на пользовательском Mac: bulk-лицензии, точечный Единый реестр лицензий, exact-number lookup медизделий без автопривязки к Company и перечень клинических организаций. Все gates 1–6 PASS; 450 tests; PostgreSQL `kontragent`; Alembic `a9c4e6f8b201`; Chromium HTTP 200, `page_errors=[]`. `auto_update=NOT_CONFIGURED`.

## W1-005
Паспорт: [ROSKOMNADZOR_SOURCE_PASSPORT.md](ROSKOMNADZOR_SOURCE_PASSPORT.md). Scope A–F утверждён. A, D, E загружены из полных официальных файлов; F прошёл live found/not_found и PostgreSQL cache. B/C не публиковались из-за повторяемого premature EOF официального сервера. Person/IP-данные изолированы в отдельной непубличной таблице. 458 tests PASS; фактический Chromium показал A=`found`, B/C=`unavailable`, ошибок страницы нет. Итоговый Six Gates остаётся NOT CONFIRMED до полных B/C. `auto_update=NOT_CONFIGURED`.

## W1-006
W1-006 НОСТРОЙ / СРО — NEXT EXECUTABLE / NOT STARTED. Это следующий шаг разработки. Перед началом сверить актуальный `main`, затем выполнять обычный source-passport / ingestion / PostgreSQL / product / browser / coverage / six-gate цикл без изменения принятой архитектуры.

## После Wave 1

Утверждённый порядок и спецификации находятся в `POST_WAVE1_PRODUCT_PLAN.md` и `WAVE_IMPLEMENTATION_PLAN.md`. До 10k SEO обязательны три независимых допуска: Security & Resilience, Legal Launch и Product / Company Card Acceptance.

## Общие ограничения
Каждый источник проходит отдельно: tests; реальный официальный ответ; PostgreSQL write/read; found/not_found/not_applicable/unavailable; настоящий Chromium; количества/даты/покрытие. Только exact identifiers. Код готов, данные загружены, PostgreSQL подтверждён, браузер проверен, покрытие измерено и auto-update настроен — разные статусы.
Person / Leads / CRM / Enterprise не начинать во время Wave 1. РНП/ЕИС отложен до официального доступа; DaMIA не подключать.

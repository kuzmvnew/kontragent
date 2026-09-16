# Wave 1 — официальный промежуточный статус

Дата: 16.09.2026. Фаза 3 завершена в согласованном объёме; Фаза 4 ACTIVE.
Это статус результатов, не изменение утверждённой архитектуры и порядка разработки.

| Шаг | Источник | Фактическое состояние |
|---|---|---|
| W1-001 | CBR Warning List | ACCEPTED / SIX GATES PASS на Mac |
| W1-002 | CBR FinOrg | ACCEPTED / SIX GATES PASS на Mac |
| W1-003 | ФНС — МСП, получатели поддержки | ACCEPTED / SIX GATES PASS на Mac |
| W1-004 | Росздравнадзор | ACCEPTED / SIX GATES PASS на Mac |
| W1-005 | Роскомнадзор | NEXT / NOT STARTED |
| W1-006 | НОСТРОЙ / СРО | NOT STARTED |

Полностью приняты на пользовательском Mac 4 источника из 6. Следующий источник — W1-005 Роскомнадзор, статус NEXT / NOT STARTED; W1-006 не запускался.

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

## W1-005—W1-006
W1-005 Роскомнадзор — NEXT / NOT STARTED. W1-006 НОСТРОЙ / СРО — NOT STARTED. Каждый требует отдельной команды и собственного source passport.

## Общие ограничения
Каждый источник проходит отдельно: tests; реальный официальный ответ; PostgreSQL write/read; found/not_found/not_applicable/unavailable; настоящий Chromium; количества/даты/покрытие. Только exact identifiers. Код готов, данные загружены, PostgreSQL подтверждён, браузер проверен, покрытие измерено и auto-update настроен — разные статусы.
Person / Leads / CRM / Enterprise не начинать. РНП/ЕИС отложен до официального доступа; DaMIA не подключать.

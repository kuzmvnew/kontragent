# Wave 1 — официальный промежуточный статус

Дата: 16.09.2026. Фаза 3 завершена в согласованном объёме; Фаза 4 ACTIVE.
Это статус результатов, не изменение утверждённой архитектуры и порядка разработки.

| Шаг | Источник | Фактическое состояние |
|---|---|---|
| W1-001 | CBR Warning List | ACCEPTED / SIX GATES PASS на Mac |
| W1-002 | CBR FinOrg | ACCEPTED / SIX GATES PASS на Mac |
| W1-003 | ФНС — МСП, получатели поддержки | ACCEPTED / SIX GATES PASS на Mac |
| W1-004 | Росздравнадзор | NEXT / NOT STARTED |
| W1-005 | Роскомнадзор | NOT STARTED |
| W1-006 | НОСТРОЙ / СРО | NOT STARTED |

Полностью приняты на пользовательском Mac 3 источника из 6. Следующая задача — только W1-004 Росздравнадзор.

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

## W1-004—W1-006
W1-004 Росздравнадзор — следующий и пока NOT STARTED. Перед реализацией установить точный официальный подреестр, машинный канал, формат, даты/обновление, exact identifiers, режим нагрузки/хранения и source passport. W1-005 и W1-006 не начинать вместе с ним.

## Общие ограничения
Каждый источник проходит отдельно: tests; реальный официальный ответ; PostgreSQL write/read; found/not_found/not_applicable/unavailable; настоящий Chromium; количества/даты/покрытие. Только exact identifiers. Код готов, данные загружены, PostgreSQL подтверждён, браузер проверен, покрытие измерено и auto-update настроен — разные статусы.
Person / Leads / CRM / Enterprise не начинать. РНП/ЕИС отложен до официального доступа; DaMIA не подключать.

# W1-001 — CBR Warning List: приёмка результата

Дата: 16.09.2026. Область: только W1-001. W1-003 этой приёмкой НЕ НАЧАТ.

## Итоговый статус

**W1-001 SIX GATES: PASS на пользовательской Mac/PostgreSQL/Chromium среде.**

Проверенный код был получен из `main` до запуска локальной приёмки:

- commit: `8937cd7bafbc67b2accd2ebf185ced8418c45d2d`;
- PR #26 — W1-001 hardening/acceptance;
- Alembic head на Mac: `e5a7f4c2b9d1`;
- локальная база: PostgreSQL `kontragent`;
- Master Registry в отчёте: **6 781 485** ЮЛ/ИП.

Приёмка выполнена командой:

```bash
uv run --with playwright==1.63.0 python -m scripts.accept_cbr_warning_list --tests --sync --browser
```

Финальный вывод локального запуска:

```text
W1-001 SIX GATES: PASS
```

`auto_update`: **NOT_CONFIGURED**. Это отдельный статус и он не считается выполненным только потому, что ручной sync прошёл успешно.

## Локальный официальный импорт на Mac — 16.09.2026

Официальный канал Банка России:

- документация: `https://www.cbr.ru/development/warning-list/`;
- полный JSON: `https://www.cbr.ru/inside/warning-list/black-list-json`;
- HTTP status: **200**;
- transport: `live_official`;
- `retrieved_at`: `2026-09-16T12:29:06.368707+00:00`;
- SHA-256: `f9b5d0af8c19abed44f6e65350a9da358c2ff53cf686bd0efb5c34c275bdc81f`.

Результат импорта в пользовательскую PostgreSQL:

- source records: **27 163**;
- imported records: **27 163**;
- inserted/current snapshot records: **27 163**;
- rejected: **0**;
- duplicate records: **0**;
- conflicting duplicates: **0**;
- complete snapshot validated: **true**;
- dataset id: **149**;
- ingestion run id: **11**;
- ledger matches snapshot: **true**;
- repeat read: **PASS**.

Состав snapshot:

- с пригодным для exact-INN сопоставления ИНН: **2 995**;
- уникальных ИНН: **2 995**;
- без пригодного ИНН: **24 168**.

Записи без ИНН не сопоставляются по похожему названию, сайту или адресу и не используются как доказательство связи с карточкой компании.

## Пересечение с Master Registry на Mac

Exact-INN join выполнен в пользовательской БД:

- Master Registry: **6 781 485** компаний/ИП;
- source records with usable INN: **2 995**;
- linked source records: **857**;
- linked unique Master Registry companies: **857**;
- source records with INN but without a Master Registry match: **2 138**.

Таким образом, покрытие по exact-INN измерено. Число 857 означает подтверждённое пересечение текущего snapshot Warning List с текущим Master Registry, а не общий процент «проверенных» компаний и не оценку безопасности остальных компаний.

## Даты и период

`data_date = 2026-09-16` в этой интеграции означает **дату получения snapshot в UTC**, а не объявленную Банком России дату состояния всего реестра.

По локальному snapshot:

- минимальная дата внесения записи: **2021-02-01**;
- максимальная дата внесения записи: **2026-09-16**;
- минимальная дата обновления записи: **2021-04-29**;
- максимальная дата обновления записи: **2026-09-16**.

## Реальная найденная компания

Контрольный exact-INN пример из локально загруженного официального snapshot:

- ИНН: `0105064330`;
- компания: `ООО "ИНВЕСТКАПИТАЛ24"`;
- CBR ID: `9806`;
- `result = found`;
- `checked = true`;
- `applicable = true`;
- `is_listed = true`;
- record count: **1**;
- matching method: `inn_exact`;
- дата внесения: `2021-06-17`;
- дата обновления: `2023-08-30`;
- признак: `Признаки нелегального кредитора`;
- официальный detail URL: `https://www.cbr.ru/inside/warning-list/detail/?id=9806`.

Повторное чтение из PostgreSQL успешно.

Интерпретация продукта остаётся ограниченной фактом источника: включение означает, что Банк России выявил признаки нелегальной деятельности на финансовом рынке; это не судебный приговор.

## Реальный `not_found` по загруженному snapshot

Контрольный ИНН: `9102309919`.

Результат:

- `checked = true`;
- `applicable = true`;
- `result = not_found`;
- `is_listed = false`;
- record count: **0**;
- matching method: `inn_exact`;
- snapshot/data date: `2026-09-16`.

Семантика: `not_found` означает только отсутствие exact-INN записи в конкретном успешно опубликованном snapshot. Это **не** означает наличие лицензии, отсутствие иных рисков или «всё хорошо».

## Неприменимость и ошибки

Источник применим и к ЮЛ, и к ИП. Поэтому отдельный бизнес-сценарий `not_applicable` для корректного ИНН ЮЛ/ИП здесь **N/A** с объяснением, а не положительный результат.

Для неизвестности/ошибки действуют отдельные состояния:

- `unavailable` / unknown при недоступном или не загруженном источнике;
- `is_listed = None` для неизвестного результата;
- некорректный идентификатор не превращается в `not_found`;
- ошибка скачивания не стирает последний успешный snapshot;
- ошибка INSERT/публикации приводит к rollback и не заменяет рабочий snapshot неполными данными.

Failure-сценарии проверяются regression tests без намеренного вывода пользовательской PostgreSQL из строя.

## Реальный браузер на Mac

Chromium через Playwright открыл реальные карточки приложения.

### `found`

- URL route: `/company/0105064330`;
- HTTP 200;
- result: `found`;
- виден блок «Предупредительный список Банка России»;
- виден exact-INN match, запись, признак, даты, адрес, сайты и ссылка на Банк России;
- `page_errors = []`;
- screenshot: `data/acceptance/w1-001/20260916T122905Z/found.png`.

### `not_found`

- URL route: `/company/9102309919`;
- HTTP 200;
- result: `not_found`;
- виден текст, что в загруженном snapshot нет exact-INN совпадения;
- показана дата получения snapshot и ограничение интерпретации;
- `page_errors = []`;
- screenshot: `data/acceptance/w1-001/20260916T122905Z/not-found.png`.

## Шесть обязательных критериев — итог

| Проверка | Результат | Доказательство |
|---|---|---|
| 1. Автоматические тесты | **PASS** | локальный acceptance запущен с `--tests`; code/CI baseline после hardening — 384 tests |
| 2. Реальная компания из официального источника | **PASS** | HTTP 200, официальный full JSON, ИНН `0105064330`, CBR ID `9806`, exact-INN `found` |
| 3. PostgreSQL write/readback | **PASS** | 27 163 строк опубликованы в PostgreSQL `kontragent`, `reread_pass=true`, ledger=snapshot |
| 4. absence / N/A / failure semantics | **PASS** | real snapshot `not_found`; N/A неприменимости объяснён; failure/rollback regression tests; unknown не подменяется `false` |
| 5. Реальная карточка в браузере | **PASS** | Chromium: `found` и `not_found`, HTTP 200, видимый текст, screenshots, `page_errors=[]` |
| 6. Количество / даты / покрытие | **PASS** | 27 163 записей; даты измерены; 2 995 usable INN; 857 exact-INN links к Master Registry из 6 781 485 |

**Итог: W1-001 полностью принят в пользовательской среде по согласованным шести критериям.**

Это не означает, что настроено автообновление. `auto_update = NOT_CONFIGURED` остаётся отдельным незавершённым статусом.

## Отдельно: более ранняя CI-проверка

До локальной приёмки PR #26 прошёл отдельный CI/PostgreSQL/Chromium стенд:

- acceptance workflow run `35094003682`: SUCCESS;
- ordinary CI run `35094003674`: SUCCESS;
- 384 tests;
- официальный импорт на момент того запуска: **27 154** записей;
- с ИНН: **2 995**;
- без ИНН: **24 159**.

Разница 27 154 → 27 163 между CI и последующим запуском на Mac отражает изменение живого официального списка между запросами; результаты двух запусков не смешиваются.

## Защита импорта

Код W1-001 после hardening:

- отвергает частично распарсенный/неконсистентный snapshot;
- отвергает конфликтующие дубли;
- публикует rows + provenance/успешный run атомарно;
- сохраняет предыдущую рабочую версию при ошибке;
- не выдаёт неизвестность за отсутствие записи;
- использует exact-INN matching как доказательство связи;
- не делает миллионы запросов по каждому ИНН: выполняется один запрос полного официального списка на явный sync.

Существующая миграция W1-001: `d4c9b1a2e7f0`. Общий Alembic head: `e5a7f4c2b9d1`.

## Повторный аудит при необходимости

Полная повторная приёмка:

```bash
uv run --with playwright==1.63.0 python -m scripts.accept_cbr_warning_list --tests --sync --browser
```

Только отчёт по текущей БД без нового sync:

```bash
uv run python -m scripts.accept_cbr_warning_list
```

Локальные отчёты и screenshots хранятся под `data/acceptance/w1-001/<UTC timestamp>/`; каталог `data` не публикуется в Git.

## Handoff после W1-001

- W1-001: **ACCEPTED — SIX GATES PASS on Mac**.
- W1-002: **LIVE + CACHE PASS** для ИНН `7707083893`; это точечная интеграция, не массовый registry import. Отдельную браузерную приёмку W1-002 не считать выполненной без её собственного доказательства.
- W1-003: **NEXT PLANNED / NOT STARTED**. Этот документ не запускает W1-003.
- RNP/EIS: отложен до официального доступа.
- DaMIA: не подключать.
- Person / Leads / CRM / Enterprise: не начинать.

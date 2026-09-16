# W1-002 — CBR FinOrg: протокол приёмки

Статус: REAL CI SIX GATES PASS; пользовательская LOCAL ACCEPTANCE PENDING.
Кодовый baseline перед задачей: de325730757884a924bb11db3e30fa4d2342191d (PR #29). Доработка приёмки: PR #30.

## Что реализовано
scripts/accept_cbr_finorg.py использует существующие provider/service, не создаёт альтернативный источник. Реальный FastAPI запускается на свободном localhost-порту, реальные страницы /company/7707083893 и /company/9102309919 открываются Chromium. Проверяются HTTP 200, видимый result, лицензии и их количество, число active по текущей логике, ограничения интерпретации и отсутствие page_errors. Сохраняются PNG и report.json.
--live выполняет только две точечные проверки (максимум три SOAP-запроса с интервалом 2 секунды), сохраняет ответы в существующую PostgreSQL и SOAP-файлы с SHA-256 в локальный каталог приёмки. Без --live источник не запрашивается, используются успешные записи текущего дня.
Скрипт не вставляет фиктивные компании в пользовательский Master Registry, не применяет миграции, не перезапускает импорты W1-001/W1-003 и не меняет Git. В пользовательском режиме требуется БД kontragent и существование обеих реальных компаний. В CI используется отдельная БД test, контекст явно обозначен.

## Фактический CI-прогон
GitHub Actions: https://github.com/kuzmvnew/kontragent/actions/runs/35129041426
Head PR: d6d7ce961b09f189a76e30be987b215f330ed128. Проверенный merge-ref: 3ac4cf67650a9306d725e6b189538d145e48992a.
16.09.2026 17:35 UTC; 435 passed; все шесть критериев PASS в отдельном Linux/PostgreSQL/Chromium окружении. До разработки acceptance полный baseline 421 тест (workflow 35128592401).

| Реальный ИНН | Официальный ответ | PostgreSQL reread | Chromium |
|---|---|---|---|
| 7707083893 — ПАО Сбербанк | found; 10 лицензий/прав, 9 active по текущей логике | PASS, cached=True | HTTP 200, visible found, page_errors=[] |
| 9102309919 — АО Крымавтодор | not_found, 0 лицензий | PASS, cached=True | HTTP 200, visible not_found, ограничение показано, page_errors=[] |

Три официальных HTTP 200. Источник: https://www.cbr.ru/FO_ZoomWS/FinOrg.asmx ; методы SearchByINNs и GetFullInfoByINN. Артефакт 10460568286 w1-002-acceptance-evidence содержит report.json, tests.log, server.log, SOAP-ответы и оба PNG. Срок хранения артефакта 7 дней; текстовый протокол остаётся в Git.
Кэш CI: 2 успешные строки, 0 ошибок, 2 контрольные Master-компании. Эти числа НЕ переносятся в пользовательский Master Registry 6 781 485.

## Локальный запуск на Mac
```bash
cd /Users/mikhailkuznetsov/Documents/kontragent &&
git pull --ff-only &&
git log -1 --oneline &&
uv run --with playwright==1.63.0 python -m playwright install chromium &&
uv run --with playwright==1.63.0 python -u -m scripts.accept_cbr_finorg --tests --live --browser
```
Скрипт создаёт data/acceptance/w1-002/<UTC timestamp>/report.json, found.png и not_found.png. Он не публикует эти локальные файлы автоматически в Git.
Успех: gates 1—6 PASS, accepted=true, user_mac_accepted=true, database=kontragent, platform=Darwin и W1-002 SIX GATES: PASS. При ошибке будет NOT ACCEPTED; непроверенное не помечается успешным.
После получения отчёта сохранить обезличенный протокол с SHA кода, датой, реальными количествами кэша/покрытия и browser evidence, затем синхронизировать WAVE1_STATUS/PROJECT_STATUS/roadmap/handoff. Пока локальный запуск не выполнен — не писать ACCEPTED on Mac.

## Шесть критериев
1. PASS на CI — 435 тестов; локальная команда повторяет полный набор.
2. PASS — реальные SearchByINNs/GetFullInfoByINN и точный ИНН.
3. PASS на CI и ранее LIVE+CACHE для Сбербанка на Mac; оба локальных случая проверяются новой командой.
4. PASS на тестах — ошибки/непроверенность не становятся not_found; N/A для отдельной бизнес-неприменимости при корректном ИНН ЮЛ/ИП. Это не движок определения необходимости лицензии.
5. PASS на реальном Chromium CI; Mac NOT CONFIRMED до локального отчёта.
6. PASS на CI для on-demand кэша; Mac NOT CONFIRMED до измерения текущего кэша и точного пересечения с Master.
Полный размер реестра ЦБ и bulk import — N/A, а не искусственно пройденный массовый импорт. Дата проверки не является датой всего реестра.
auto_update=NOT_CONFIGURED. Отдельное расписание не добавляется этой задачей.

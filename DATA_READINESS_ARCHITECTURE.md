# Data Readiness Architecture

Дата: 17.09.2026  
Статус: implemented foundation

## Назначение

Data Readiness расширяет существующие `DataSource`, `DataSet` и `IngestionRun`. Второго каталога источников нет. Слой отвечает за эксплуатационное состояние данных, а не за риск компании: `STALE` не означает плохую компанию, `ERROR` не означает отсутствие факта, а недоступный источник не превращается в `not_found`.

## Типы datasets

| Тип | Политика |
|---|---|
| `bulk_snapshot` | download → integrity → parse → staging → checks → atomic publish |
| `on_demand_api` | только точечный запрос, датированный cache, provider health/quota/budget |
| `public_live_user_triggered` | внешний запрос только после явного действия пользователя |
| `human_assisted` | пользовательская сессия; CAPTCHA/challenge не обходятся |
| `source_blocked` | редкий probe с bounded backoff; последняя успешная версия сохраняется |
| `access_credential_pending` | probe не запускается до официального доступа/credentials |

Checko, NPD и суды остаются user-triggered. CBR FinOrg, NOSTROY и NOPRIZ не получают массового crawl. FNS BANKINFORM и CBR ZSK зарегистрированы как human-assisted. Roskomnadzor B/C и защищённый НРС НОСТРОЙ представлены как source-blocked. `eis_rnp` и существующий EIS dataset представлены как access-pending; DaMIA автоматически не включается.

## Operational model

`DataSet` хранит source/dataset identity, dataset kind, update mode, freshness policy/threshold, `last_attempt_at`, `last_success_at`, `source_as_of`, `retrieved_at`, `checked_at`, `published_at`, record count, coverage, operational status, last error/time, retry count/time, next expected update and honest auto-update status.

Статусы: `CURRENT`, `STALE`, `UPDATING`, `ERROR`, `UNAVAILABLE`, `SOURCE_BLOCKED`, `ACCESS_PENDING`, `MANUAL`, `USER_TRIGGERED`, `NOT_CONFIGURED`.

`is_dataset_stale` предпочитает `source_as_of`, затем `last_success_at`. Daily/weekly/monthly имеют консервативные пороги 36 часов / 9 дней / 40 дней. Irregular, manual и trigger-based datasets не получают выдуманную частоту. Ошибка хранится независимо от freshness.

## Run history и observability

`IngestionRun` сохраняет старые записи и дополнен UUID, trigger, publisher/retrieval timestamps, seen/written/rejected, duplicates/conflicts, checksum/version, stable error code, duration and lock owner. Старые поля сохранены для совместимости.

Structured logging использует run/source/dataset/status/duration/record/error-type поля. `safe_error_message` удаляет значения API key/token/cookie/authorization; raw private records не логируются и не выводятся в internal API.

## Bulk safety

`BulkUpdatePipeline` выполняет потоковую SHA-256 проверку, минимальный размер, ZIP central-directory/EOF и CRC, optional file count, parse validation и только затем staging. Domain publish callback и переключение active `DatasetPublication` выполняются в одной транзакции. Любое исключение откатывает staging/domain changes, поэтому предыдущая active publication остаётся активной. История публикаций неизменяема; active pointer уникален на dataset.

Existing importers уже используют транзакционные replace/staging схемы. Новая pipeline является единым контрактом для их постепенного подключения; большие импорты в этом этапе не повторялись.

## Locking и recovery

`dataset_update_locks` имеет один PK на dataset, owner, acquired/expires timestamps. Конкурентная вставка закрывается unique constraint; существующая непросроченная чужая блокировка отклоняет запуск; истёкшая блокировка атомарно переиспользуется под row lock. Lock снимается в `finally`.

## Retry

Network timeout, network error и 429 допускают bounded exponential backoff с учётом `Retry-After`. 403/protection и incomplete protected artifacts становятся `SOURCE_BLOCKED`; credential failure становится `ACCESS_PENDING`. Максимальная частота ограничена, protection не обходится, последняя production snapshot не удаляется.

## Internal projection

- HTML: `/internal/data-readiness`
- JSON: `/internal/api/data-readiness`

Страница не связана с SEO navigation, отдаёт `noindex/noarchive` и `no-store`, показывает разные status colors, global summary, все datasets и последние 50 runs. Ошибки санитизируются. Credentials, cookies, tokens и private raw records не выдаются.

## Portability

Источник истины — PostgreSQL, а update handler — обычный Python callable. Поэтому будущий доверенный Windows worker сможет выполнять те же jobs, не меняя registry/run/lock contracts. Windows, hardware, remote access и self-hosted runner в этот этап не входят.

# Data Readiness Scheduler Decision

Дата: 17.09.2026  
Решение: database-driven simple worker loop

## Рассмотренные варианты

| Вариант | Решение |
|---|---|
| system cron | Полезен как внешний supervisor/one-shot trigger, но сам не решает due selection, locks, history и restart state. |
| APScheduler | Сейчас добавляет dependency и in-process state без необходимого выигрыша. Можно пересмотреть при появлении сложных календарей. |
| simple worker loop | Выбран: минимален, переносим между Mac/Windows и использует PostgreSQL как durable state. |
| Celery/RabbitMQ/Redis | Не принят: для solo/MVP нет подтверждённой потребности в broker/distributed queue. |

## Реализация

`scripts/run_data_readiness_scheduler.py` поддерживает one-shot запуск и supervised loop. `due_dataset_codes` выбирает только enabled datasets с `auto_update_status=CONFIGURED`, наступившим expected/retry time. Registry handlers отделён от loop.

Scheduler не выбирает manual, user-triggered, human-assisted, source-blocked или access-pending datasets. Handler обязан использовать dataset lock и сохранять run history; `BulkUpdatePipeline` уже выполняет оба требования. После restart due state читается из PostgreSQL, а stale locks восстанавливаются по expiry.

## Честный deployment status

На 17.09.2026 worker не установлен как macOS/Windows service и production handlers не объявлены развёрнутыми. Поэтому bulk datasets имеют `auto_update_status=NOT_CONFIGURED`, даже если их текущие snapshots доступны. Это намеренно: наличие кода scheduler не равно работающему расписанию.

Перевод конкретного dataset в `CONFIGURED` разрешён только одновременно с:

1. зарегистрированным production handler;
2. supervised deployment или cron one-shot;
3. timeout/retry policy;
4. lock/run-history checks;
5. успешным acceptance run и alert path.

Такой порядок позволяет позже разместить тяжёлые bulk handlers на доверенном домашнем Windows worker без привязки архитектуры к одному Mac process.

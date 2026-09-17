# Data Readiness Acceptance

Дата проверки: 17.09.2026  
Ветка: `codex/data-readiness`  
Статус: acceptance evidence complete; PR/CI pending at document creation

## Baseline и migration

- clean `main`, local/origin: `3be6fcfcae551e47a7a44a0324a43e086732cf07`;
- исходный baseline: `534 passed`;
- исходный Alembic: `c3d4e5f6a7b8 (head)`;
- migration path проверен `upgrade → downgrade → upgrade`;
- новый head: `d4e5f6a7b8c9`;
- full regression после реализации: `556 passed`.

Большие source imports не повторялись.

## Automated acceptance

`tests/test_data_readiness.py` покрывает 22 сценария: scheduled и manual success, failed download, bad checksum, partial file, ZIP EOF/CRC, bad parse, atomic rollback, cache reuse, retry/backoff/cap, 429/Retry-After, 403/protection, stale/fresh, on-demand cache TTL, collision, stale-lock contract, distinct manual/human/user/access/blocked modes и secret redaction.

## PostgreSQL evidence

После миграции registry содержит 44 datasets. Наблюдаемый summary:

- current: 11;
- stale: 3;
- source_blocked: 3;
- access_pending: 2;
- manual: 6;
- user_triggered: 12;
- not_configured: 7;
- updating/error: 0.

Реальные категории:

| Категория | Dataset | Evidence |
|---|---|---|
| bulk | `fns_sme_support` | current; source as of 15.09.2026; 11,925,998 written; historical success/failure runs preserved |
| on-demand | `cbr_finorg` | `on_demand_api`, `USER_TRIGGERED`; массовый crawl отсутствует |
| public live | `fns_npd`, `checko_arbitration_cases` | `public_live_user_triggered`; GET/search/background не вызывают provider |
| human assisted | `fns_account_suspension`, `cbr_zsk` | `MANUAL/HUMAN_ASSISTED`; challenge не обходится |
| blocked | `rkn_broadcast_licenses`, `rkn_registered_media` | `SOURCE_BLOCKED`; не `not_found` |
| access pending | `eis_rnp` | `ACCESS_PENDING`; credentials/DaMIA не обходятся |

Legacy successful runs получили read-only operational projection; неизвестное legacy coverage помечено честной note и будет заменено структурированными counters следующим validated publish.

Реальная PostgreSQL lock-проверка на `excel_companies`: first acquire `true`, конкурентный owner отклонён, после expiry другой owner успешно восстановил stale lock; acceptance lock удалён.

## API и browser

FastAPI поднят локально на `127.0.0.1:8766`. В реальном in-app Chromium открыта `/internal/data-readiness`: HTTP page rendered, 44 rows, summary/status colors, source/dataset metadata and recent run history visible. Проверены `fns_sme_support` current, CBR warning stale, Checko/NPD user-triggered, BANKINFORM/ZSK manual, Roskomnadzor blocked и EIS access-pending. Страница содержит noindex meta/header и не добавлена в public navigation.

## Boundaries

- Ни один dataset не помечен `CONFIGURED` до фактического deployment handler/supervisor.
- Source failure и company `not_found` не смешиваются.
- Risk Engine, Summary Engine, Company Card v2, Report v1, Wave 2 и SEO не начинались.
- Никакие API keys/tokens/cookies не добавлены в Git, DB, browser или logs.

## Final gate

Перед merge обязательны `git diff --check`, повторный full pytest и зелёный GitHub Actions. Merge не выполняется этой веткой до завершения acceptance/CI.

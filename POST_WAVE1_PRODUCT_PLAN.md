# POST-WAVE1 PRODUCT PLAN — Kontragent

Status: APPROVED PRODUCT EXECUTION PLAN
Date: 2026-09-17

Этот документ фиксирует согласованный порядок работ после Wave 1 и дополняет `ROADMAP.md`, `ROADMAP_DETAILED.md` и `WAVE_IMPLEMENTATION_PLAN.md`. Архитектура проекта не переписывается.

## Текущее состояние Wave 1

На актуальном `main` commit `911bc8d211beb68deccf19b8df54d197ccd6aeda`:

- W1-001 CBR Warning List — ACCEPTED / SIX GATES PASS;
- W1-002 CBR FinOrg — ACCEPTED / SIX GATES PASS;
- W1-003 ФНС МСП/получатели поддержки — ACCEPTED / SIX GATES PASS;
- W1-004 Росздравнадзор — ACCEPTED / SIX GATES PASS;
- W1-005 Роскомнадзор — IN PROGRESS / SOURCE-BLOCKED (B, C); A/D/E/F реализованы и проверены, но Six Gates не объявлен;
- W1-006 НОСТРОЙ / СРО — NEXT EXECUTABLE / NOT STARTED.

W1-006 разрешено выполнять, пока W1-005 ждёт восстановления/полного ответа официальных B/C-источников. Это не означает закрытие W1-005. Wave 1 считается закрытой только после W1-006 acceptance и отдельного решения по оставшемуся blocker W1-005.

## Утверждённый порядок после Wave 1

1. Зафиксировать полный продуктовый каталог функций из наших обсуждений и реальных ТЗ.
2. Закрыть Auto-update / Data Readiness: расписания, last_success, source_as_of, coverage, ошибки, retry/backoff, статус источников и журнал загрузок.
3. Реализовать Company Card v2 как проверку контрагента, а не каталог сырых государственных данных.
4. Реализовать расширяемые Risk Engine и Summary Engine по отдельным спецификациям.
5. Реализовать Report v1: PDF должной осмотрительности; затем табличный export.
6. Пройти обязательный `SECURITY & RESILIENCE GATE`.
7. Пройти обязательный `LEGAL LAUNCH GATE`.
8. Пройти обязательный `PRODUCT / COMPANY CARD ACCEPTANCE GATE` с полным end-to-end и data/risk/summary acceptance.
9. Только после трёх Gates открыть первые 10 000 качественных SEO-карточек компаний.
10. Реализовать `Lists + Bulk Check`: списки, XLSX/CSV, вставка ИНН, массовая проверка, фильтры, export.
11. Реализовать `Monitoring / Event Engine` и расширяемую модель уведомлений.
12. Реализовать лёгкий `Workspace v1`: организация-клиент, пользователи, private/shared lists, notes, history.
13. Только после этого переходить к Wave 2 источников, используя Source Access & Cost Gate для каждого нового источника.
14. После Company B2B Core — Person Core.
15. После Person Core — отдельный Compliance-модуль.
16. Затем — API / Enterprise / SSO / SLA / внутренние интеграции / госзакупочная готовность.

## Главный архитектурный принцип

Новый источник не должен заставлять переписывать Risk Engine, Summary Engine, Monitoring, Bulk Check или карточку.

Цепочка:

`Source -> Evidence -> Fact -> Applicability -> Derived Metrics -> Risk Rules -> Section Assessment -> Coverage -> Summary -> Event -> Notification`

Новый источник добавляет Evidence, Facts, Derived Metrics, Events и при необходимости новые версии правил. Старые оценки сохраняются с версией методики.

## Обязательные спецификации

- `PRODUCT_FUNCTION_CATALOG.md`
- `RISK_ENGINE_SPEC.md`
- `SUMMARY_ENGINE_SPEC.md`
- `FINANCIAL_DISTRESS_SPEC.md`
- `COURT_INTELLIGENCE_SPEC.md`
- `EVENT_NOTIFICATION_MODEL.md`
- `TRANSPORT_SOURCE_AUDIT.md`
- `SOURCE_ACCESS_COST_GATE.md`
- `SECURITY_RESILIENCE_GATE.md`
- `LEGAL_LAUNCH_GATE.md`
- `PRODUCT_CARD_ACCEPTANCE_GATE.md`

## Guardrails

- Wave 2 не начинать до выполнения перечисленных выше продуктовых этапов и первого 10k SEO release.
- Не считать отсутствие данных отсутствием риска.
- `not_found`, `not_applicable` и `unavailable` имеют разный смысл.
- Risk и Coverage считаются отдельно.
- Никаких формулировок «существенно», «много», «быстро растёт» без исходных чисел, периода и расчёта, когда такой расчёт возможен.
- Судебные требования не равны подтверждённому долгу.
- Прогноз не равен факту.
- Person и чувствительные данные не публикуются автоматически в SEO/public API.
- Любой новый источник сначала проходит проверку доступа, прав, стоимости и коммерческого использования.

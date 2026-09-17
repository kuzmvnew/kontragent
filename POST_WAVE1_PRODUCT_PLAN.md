# POST-WAVE1 PRODUCT PLAN — Kontragent

Status: APPROVED PRODUCT EXECUTION PLAN
Date: 2026-09-17

Этот документ фиксирует согласованный порядок работ после Wave 1 и дополняет `ROADMAP.md`, `ROADMAP_DETAILED.md` и `WAVE_IMPLEMENTATION_PLAN.md`. Архитектура проекта не переписывается.

## Wave 1 final state

Wave 1 официально закрыта и принята отдельным решением владельца продукта.

Authoritative decision: `WAVE1_CLOSURE_DECISION.md`.

Фактические статусы источников сохранены без ретуширования:

- W1-001 CBR Warning List — ACCEPTED / SIX GATES PASS;
- W1-002 CBR FinOrg — ACCEPTED / SIX GATES PASS;
- W1-003 ФНС МСП/получатели поддержки — ACCEPTED / SIX GATES PASS;
- W1-004 Росздравнадзор — ACCEPTED / SIX GATES PASS;
- W1-005 Роскомнадзор — DEFERRED EXCEPTION / SOURCE-BLOCKED (B, C); A/D/E/F реализованы и проверены, B/C остаются `unavailable`;
- W1-006 НОСТРОЙ / НОПРИЗ / СРО — ACCEPTED / SIX GATES PASS.

Wave 1 closure does not imply auto-update readiness and does not convert W1-005 B/C into success.

## Утверждённый порядок после Wave 1

1. **Intermediate Stage 1.5** — закрыть незавершённую приёмку старых источников и добавить/восстановить критические бесплатные источники, без которых Risk Engine будет строиться на неполной основе.
2. Закрыть Auto-update / Data Readiness: расписания, last_success, source_as_of, полнота проверки, ошибки, retry/backoff, статус источников и журнал загрузок.
3. Реализовать полный Risk Engine на проверенном наборе фактов Stage 1.5.
4. Реализовать Summary Engine поверх Risk Engine и Evidence/Facts.
5. Собрать Company Card v2 как финальный пользовательский слой, а не как место расчёта риска.
6. Реализовать Report v1: PDF должной осмотрительности; затем табличный export.
7. Пройти обязательный `SECURITY & RESILIENCE GATE`.
8. Пройти обязательный `LEGAL LAUNCH GATE`.
9. Пройти обязательный `PRODUCT / COMPANY CARD ACCEPTANCE GATE` с полным end-to-end и data/risk/summary acceptance.
10. Только после трёх Gates открыть первые 10 000 качественных SEO-карточек компаний.
11. Реализовать `Lists + Bulk Check`: списки, XLSX/CSV, вставка ИНН/ОГРН, массовая проверка, фильтры, export.
12. Реализовать `Monitoring / Event Engine` и расширяемую модель уведомлений.
13. Реализовать лёгкий `Workspace v1`: организация-клиент, пользователи, private/shared lists, notes, history.
14. Только после этого переходить к Wave 2 источников, используя Source Access & Cost Gate для каждого нового источника.
15. После Company B2B Core — Person Core.
16. После Person Core — отдельный Compliance-модуль.
17. Затем — API / Enterprise / SSO / SLA / внутренние интеграции / госзакупочная готовность.

## Stage 1.5 — обязательный промежуточный этап

Подробный scope и критерии: `INTERMEDIATE_STAGE_1_5.md`.

Stage 1.5 включает:

- финальную six-gate приёмку ФНС Tax Debt;
- финальную six-gate приёмку ФНС Tax Offences;
- инвентаризацию/восстановление ранее сделанного ФССП;
- инвентаризацию/восстановление ранее сделанного Федресурса/ЕФРСБ;
- Courts v1: арбитраж + суды общей юрисдикции;
- стадийную модель банкротства и ликвидации;
- решения ФНС о приостановлении операций по счетам;
- техническое исследование/рабочий режим публичной проверки высокой группы риска Банка России;
- бесплатное корпоративное раскрытие;
- нормализацию сайтов, телефонов, email, адресов, публичных банковских реквизитов и массовости адреса/руководителя/учредителя там, где есть подтверждённые данные.

Stage 1.5 — не Wave 2. Он закрывает минимально необходимый фундамент для корректного Risk/Summary/Card.

## Главный архитектурный принцип

Новый источник не должен заставлять переписывать Risk Engine, Summary Engine, Monitoring, Bulk Check или карточку.

Цепочка:

`Source -> Evidence -> Fact -> Applicability -> Derived Metrics -> Risk Rules -> Section Assessment -> Полнота проверки -> Summary -> Event -> Notification`

Новый источник добавляет Evidence, Facts, Derived Metrics, Events и при необходимости новые версии правил. Старые оценки сохраняются с версией методики.

## Company Card v2

Detailed scope: `COMPANY_CARD_V2_SCOPE.md`.

Card v2 must include, where reliable data exists:

- company identity and registration facts;
- websites, phones, email with source/date;
- registered/legal address, factual address only when supported, postal address;
- mass-address context with uncertainty for multi-tenant buildings;
- current/historical management and owners;
- mass director/founder facts with concrete counts and explanation;
- financials and explainable ratios;
- tax blocks including debt, penalties, fines, offences, paid taxes, regimes, reporting facts and dates;
- publicly disclosed bank details;
- FNS bank-account suspension decisions as a separate check;
- CBR/sector/regulatory checks including the public Bank of Russia high-risk check when available;
- courts, enforcement, bankruptcy/liquidation timeline;
- corporate disclosure;
- relationships/environment with explicit reason for each link;
- Risk, Summary, sources/dates and `Полнота проверки`.

## Free-first source policy

For Stage 1.5 and later Wave 2 planning:

- use official/open/public free sources first;
- use targeted on-demand/cache retrieval where bulk API does not exist;
- do not buy a paid API merely because it is convenient;
- a paid source requires a separate explicit commercial decision;
- public access does not remove the need to verify automation/storage/republication conditions;
- low-load/cache-first collection is preferred to aggressive crawling.

Corporate disclosure plan: `CORPORATE_DISCLOSURE_SOURCE_PLAN.md`.

## Обязательные спецификации

- `INTERMEDIATE_STAGE_1_5.md`
- `PRODUCT_FUNCTION_CATALOG.md`
- `COMPANY_CARD_V2_SCOPE.md`
- `RISK_ENGINE_SPEC.md`
- `SUMMARY_ENGINE_SPEC.md`
- `FINANCIAL_DISTRESS_SPEC.md`
- `COURT_INTELLIGENCE_SPEC.md`
- `EVENT_NOTIFICATION_MODEL.md`
- `CORPORATE_DISCLOSURE_SOURCE_PLAN.md`
- `TRANSPORT_SOURCE_AUDIT.md`
- `SOURCE_ACCESS_COST_GATE.md`
- `SECURITY_RESILIENCE_GATE.md`
- `LEGAL_LAUNCH_GATE.md`
- `PRODUCT_CARD_ACCEPTANCE_GATE.md`

## Guardrails

- Wave 2 не начинать во время Stage 1.5 и до выполнения перечисленных продуктовых этапов.
- Не считать отсутствие данных отсутствием риска.
- `not_found`, `not_applicable` и `unavailable` имеют разный смысл.
- Risk и `Полнота проверки` считаются отдельно.
- Никаких формулировок «существенно», «много», «быстро растёт» без исходных чисел, периода и расчёта, когда такой расчёт возможен.
- Судебные требования не равны подтверждённому долгу.
- Ликвидация не равна банкротству.
- Прогноз не равен факту.
- Person и чувствительные данные не публикуются автоматически в SEO/public API.
- FSSP/Fedresurs сначала инвентаризируются в текущем/историческом/локальном проекте; работа не дублируется без необходимости.
- Любой платный источник — только отдельное решение после проверки бесплатных вариантов.

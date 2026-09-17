# PROJECT STATUS — Kontragent

Последнее обновление: 17.09.2026

## Источники истины

- Код: GitHub repository `kuzmvnew/kontragent`
- Порядок разработки: `ROADMAP.md`
- Активный порядок после Wave 1: `WAVE_IMPLEMENTATION_PLAN.md`
- Wave 1 closure: `WAVE1_CLOSURE_DECISION.md`
- Текущий промежуточный этап: `INTERMEDIATE_STAGE_1_5.md`
- Handoff этапа 1.5: `STAGE_1_5_HANDOFF.md`
- Детальный план: `ROADMAP_DETAILED.md`
- Person sources/access matrix: `PERSON_CHECK_SOURCES.md`
- Текущее состояние: `PROJECT_STATUS.md`

## Правило работы

Перед крупной задачей сверять `PROJECT_STATUS.md`, `WAVE_IMPLEMENTATION_PLAN.md`, `ROADMAP.md` и актуальный код. Не повторять завершённую работу и не менять утверждённый порядок фаз без отдельного решения Михаила. После изменения кода: CI/tests → реальная проверка, если нужна → обновление статуса → GitHub.

При приёмке источника всегда отдельно проверять шесть постоянных критериев: tests → реальный официальный/публичный ответ → PostgreSQL write/read → корректная семантика состояний → реальная карточка в браузере → количество/даты/покрытие. Наличие кода, загруженных данных, проверенной карточки и настроенного автообновления — разные статусы.

## Где проект сейчас

Current Phase:

Phase 4 / Intermediate Stage 1.5

Status:

ACTIVE / IN PROGRESS

| Фаза / этап | Статус | Текущее состояние |
|---|---|---|
| 1. Фундамент | ✅ | FastAPI, PostgreSQL, Git, тестовая база |
| 2. Master Registry | ✅ | Единый реестр ЮЛ/ИП по ИНН |
| 3. Официальные источники | ✅ ЗАВЕРШЕНА | MVP CORE завершён; РНП/ЕИС отложен до официального доступа |
| Wave 1 | ✅ CLOSED / ACCEPTED | W1-001/002/003/004/006 accepted; W1-005 B/C — deferred external-source exception |
| Intermediate Stage 1.5 | ▶️ ACTIVE / IN PROGRESS | Tax Debt/Tax Offences accepted; FSSP inventory `NOT_FOUND`; remaining work follows the approved order |
| 5. Проверки и риски | 🟡 фундамент есть | Контракты checks/evidence уже заложены; полный Risk Engine после Stage 1.5 |
| 6. Карточка компании | 🟡 частично | Финальная Company Card v2 после Risk + Summary |
| 7. API и интерфейс | 🔵 далее | После ядра данных |
| Security & Resilience Gate | 🔵 обязательно | До массового публичного/SEO-релиза |
| Legal Launch Gate | 🔵 обязательно | До массового публичного/SEO-релиза |
| Product / Company Card Acceptance Gate | 🔵 обязательно | До массового публичного/SEO-релиза |
| 8. SEO | 🔵 далее | Первые 10k только после трёх Gates |
| 9. Лендинг и привлечение | ⏸ | Пауза до готовности ядра/карточки |
| 10. Монетизация | 🔵 позже | После работающего продукта |

## Wave 1 closure

Wave 1 официально закрыта 17.09.2026 отдельным решением владельца продукта.

Authoritative document: `WAVE1_CLOSURE_DECISION.md`.

Финальный статус:

- W1-001 CBR Warning List — ACCEPTED / SIX GATES PASS;
- W1-002 CBR FinOrg — ACCEPTED / SIX GATES PASS;
- W1-003 ФНС — МСП, получатели поддержки — ACCEPTED / SIX GATES PASS;
- W1-004 Росздравнадзор — ACCEPTED / SIX GATES PASS;
- W1-005 Роскомнадзор — DEFERRED EXCEPTION / SOURCE-BLOCKED (B, C); A/D/E/F verified; B/C остаются `unavailable`;
- W1-006 НОСТРОЙ / НОПРИЗ / СРО — ACCEPTED / SIX GATES PASS.

W1-005 B/C не считаются успешной проверкой и не превращаются в `not_found`. Возврат к ним после восстановления официального источника не переоткрывает Wave 1 автоматически.

## Phase 3 Result

Phase 3 completed successfully.

Official datasets integrated / product foundation present:

- Master Registry
- Tax Debt
- Tax Offences
- Paid Taxes
- Revenue & Expenses
- Average Employees
- SME Registry
- SNR
- SNRIP
- Disqualified Persons
- NPD
- ERKNM

Deferred:

RNP / EIS

Status:

WAITING FOR OFFICIAL EIS ACCESS

Decision:

Official EIS remains the only Source of Truth.

Deferred source does not block current development.

## Current Stage 1.5

Stage 1.5 is the mandatory bridge between Wave 1 and the full Risk Engine.

Detailed scope: `INTERMEDIATE_STAGE_1_5.md`.

Current work items:

1. FNS Tax Debt — ACCEPTED / SIX GATES PASS; protocol `docs/STAGE_1_5_A1_TAX_DEBT_ACCEPTANCE.md`.
2. FNS Tax Offences — ACCEPTED / SIX GATES PASS; protocol `docs/STAGE_1_5_A2_TAX_OFFENCE_ACCEPTANCE.md`.
3. FSSP — ✅ inventory complete: `NOT_FOUND`; only disabled catalog metadata exists, no implementation/data/runs.
4. Fedresurs/EFРSB — ✅ inventory complete: `NOT_FOUND`; only disabled catalog metadata/specification exists, no implementation/data/runs.
5. Bankruptcy/liquidation — ✅ normalized 15-state event model accepted; migration `f8b9c0d1e2f3`; no unsupported source events fabricated. Protocol: `docs/STAGE_1_5_D_LEGAL_EVENTS_ACCEPTANCE.md`.
6. Arbitration Courts v1 — ⛔ `PAID ACCESS DECISION REQUIRED`: official/free machine path rechecked and remains blocked; Casebook, Focus, Checko, SPARK, and Seldon compared without purchase, registration, contract, or paid dependency. See `COURTS_V1_ACCESS_RESEARCH.md`.
7. Courts of General Jurisdiction v1 — ⛔ `TECHNICAL_ACCESS_UNCONFIRMED / SOURCE_BLOCKED FOR COMPANY MATCHING`: official UI/instructions found, but BSR machine access and exact INN/OGRN matching were not established. See `COURTS_GENERAL_STAGE_1_5_ACCESS.md`.
8. FNS account-suspension decisions — ⛔ `SOURCE_BLOCKED FOR UNATTENDED MACHINE ACCESS`: one real dated official negative response confirmed; subsequent request and direct endpoint require CAPTCHA. See `FNS_ACCOUNT_SUSPENSION_STAGE_1_5_ACCESS.md`.
9. Bank of Russia public high-risk/KYC probe — ✅ `PROBE COMPLETE / PRODUCTION MACHINE MODE BLOCKED`: exact meaning and CAPTCHA/fingerprint boundary documented; no bypass. See `CBR_ZSK_STAGE_1_5_TECHNICAL_PROBE.md`.
10. Free-first corporate disclosure — ✅ `ACCEPTED / SIX GATES PASS FOR PRIME FOUNDATION`; migration `a1c2d3e4f5b6`. See `docs/STAGE_1_5_G_CORPORATE_DISCLOSURE_ACCEPTANCE.md`.
11. Company facts — 🟡 `FOUNDATION ACCEPTED / SOURCE COVERAGE PARTIAL`; migration `b2c3d4e5f6a7`, real source-backed registered/postal addresses accepted; remaining fact types need sources. See `docs/STAGE_1_5_H_COMPANY_FACT_NORMALIZATION.md`.
12. Stage 1.5 acceptance review.

Stage 1.5 is NOT Wave 2.

Stage 1.5 remains **ACTIVE / NOT COMPLETE**. C1 does not meet its completion criteria and no paid source has been approved; C2 and E also remain blocked as production machine workstreams.

## Current Release Goal

Release 1

SEO MVP

Priority:

Highest

Approved path before SEO:

`Stage 1.5 -> Auto-update/Data Readiness -> Risk Engine -> Summary Engine -> Company Card v2 -> Report v1 -> Security Gate -> Legal Gate -> Product/Card Acceptance -> 10k SEO`

## Current Development Rule

Current project priority:

Release First, but not before the agreed quality/data gates.

No approved release may be delayed by unrelated backlog. Stage 1.5 is not unrelated backlog: it is an approved prerequisite for Risk/Summary correctness.

## Текущее техническое состояние

- Python / FastAPI: ✅
- PostgreSQL: ✅
- Git / GitHub main: ✅
- GitHub Actions CI: ✅
- W1-006 full regression: **472 passed**.
- PostgreSQL database: `kontragent`.
- Alembic at Wave 1 closure: `e7a8b9c0d1e2`.
- Master Registry observed at Wave 1 closure on user Mac: **6 781 487** entities.
- W1-006 Chromium: three real company cards HTTP 200 / `page_errors=[]`.
- Private SRO person evidence: `PRIVATE_INTERNAL`; public exposure confirmed `0`.
- Auto-update remains a separate status and is not implied by Wave 1 closure.

Historical acceptance checkpoints remain valid in their own dated reports. Older Master Registry counts in those reports are not overwritten by the later Wave 1 closure count.

## Master Registry

Master Registry remains the canonical entity base.

At Wave 1 closure the user Mac reported **6 781 487** entities.

Earlier W1 acceptance reports may contain the prior dated value **6 781 485**; those numbers remain historical evidence for those specific runs.

Excel/import helper sources remain auxiliary for coverage/development and do not replace official Sources of Truth.

## Завершённые источники текущего блока Фазы 3

### ФНС SNR / SNRIP — специальные налоговые режимы

✅ Полный продуктовый путь завершён: ingestion → массовый импорт → service → aggregator → карточка → sources_used → tests → реальные ЮЛ/ИП.

Зафиксированный импорт на 01.08.2026:

- SNR ЮЛ: 1 331 461 документов, 1 137 578 компаний;
- SNRIP ИП: 4 819 031 документов, 4 818 603 уникальных ИНН, 4 535 599 ИП;
- 428 повторных документов SNRIP проверены, различий в режимах у дублей нет.

### ФНС — Реестр дисквалифицированных лиц

✅ **Источник №9 завершён.**

Официальный snapshot: 13.09.2026.

- обработано: **8 202**;
- с ИНН организации: **2 936 (35,8%)**;
- уникальных ИНН организаций: **2 774**;
- совпали с текущим Master Registry: **362 (13,05%)**;
- связанные строки: **384**.

Реальная продуктовая проверка успешна для ИНН `0205008219`: `found`, linked records 1, active records 1, `fns_disqualified` в `sources_used`.

Семантика: ИНН организации в записи ФНС связывает правонарушение с организацией, но не доказывает, что дисквалифицированное лицо является текущим руководителем. Fuzzy matching по ФИО/названию не используется.

### ФНС — НПД

✅ **Источник №10 завершён.**

Source passport: `NPD_SOURCE_PASSPORT.md`.

Режим: **PUBLIC_LIVE / ON_DEMAND**, без массового обхода.

Реальная проверка 15.09.2026:

- ИНН ИП: `784305462631`;
- HTTP 200;
- `result: not_found`;
- `is_npd: False`;
- повторное чтение: `cached: True`.

### Генпрокуратура — ФГИС ЕРКНМ (248-ФЗ)

✅ **Источник №11 завершён.**

Source passport: `ERKNM_SOURCE_PASSPORT.md`.

Режим: **OPEN_DATA / BULK XML** через официальный `proverki.gov.ru`.

Реальный январский dataset 2026, версия 15.09.2026:

- ZIP ~216 MB, XML ~1.61 GB;
- обработано / вставлено: **172 417 / 172 417**;
- duplicate ERPID: **0**;
- с ИНН субъекта: **155 555 (90,22%)**;
- с ОГРН/ОГРНИП: **155 358 (90,11%)**;
- связанных с Master Registry: **99 063 (57,46%)**;
- уникальных master-компаний с мероприятиями: **73 452**;
- конфликтов ИНН/ОГРН: **0**.

Реальный product smoke-test:

- ИНН `3123061506`;
- компания `ООО "Р - ДАРЛИНГ"`;
- `Result: found`;
- найдено **25** мероприятий;
- `sources_used` содержит `erknm_inspections`.

Сам факт контрольного/профилактического мероприятия не трактуется как негативный сигнал.

## Отложенный источник №12: РНП / ЕИС

⏸ **WAITING FOR EIS ACCESS / внешний blocker.**

Source passport: `RNP_EIS_SOURCE_PASSPORT.md`.
Privacy/load policy: `docs/RNP_EIS_PRIVACY_LOAD_POLICY.md`.

Подтверждено:

- официальный машинный канал существует: `getDocsIP` / `getDocsLE`;
- старый FTP не используется;
- scraping публичного HTML-поиска не используется;
- credential-safe provider, SOAP request builders, response parser, archive downloader и live-probe уже реализованы;
- регистрация получателя через ЕСИА начата;
- ЕИС также показала требование квалифицированного сертификата ключа проверки электронной подписи для доступа в кабинет потребителя машиночитаемых данных.

### Решение по DaMIA

DaMIA изучена как возможный резервный bridge через `API-Закупки`, но принято решение:

- сейчас DaMIA не подключать;
- не покупать временный API ради обхода blocker ЕИС;
- не строить базу РНП на DaMIA;
- оставить DaMIA только как резервный bridge/fallback на будущее;
- официальный ЕИС остаётся source of truth.

### Privacy / load

Зафиксированы обязательные правила:

- персональные данные физических лиц из РНП не публиковать в публичной карточке, API, SEO, выгрузках или поиске;
- не хранить лишние person-поля, если они не нужны для строгого matching;
- matching только по ИНН/ОГРН/ОГРНИП;
- LOW LOAD / cache / backoff до подтверждения иных официальных лимитов.

## Незакрытые исторические пункты, перенесённые в Stage 1.5

Фаза 3 и Wave 1 закрыты в согласованном объёме, но это не отменяет незакрытую приёмку двух старых налоговых источников.

### ФНС налоговая задолженность

Статус: ✅ **ACCEPTED / SIX GATES PASS** 17.09.2026.

Протокол: `docs/STAGE_1_5_A1_TAX_DEBT_ACCEPTANCE.md`.

Подтверждены официальный snapshot ФНС `7707329152-debtam`, SHA-256, parser fail-closed, 625 395 PostgreSQL snapshots / 2 031 361 items, отдельные недоимка/пени/штрафы, real found, dated not_found, IP not_applicable, unavailable contracts, Chromium и coverage. Auto-update остаётся `NOT_CONFIGURED`. Обычная опубликованная задолженность не смешивается с отдельным фактом передачи пороговой задолженности приставу.

Принятые критерии:

- tests;
- реальный found;
- корректный dated not_found внутри покрытия;
- not_applicable/unavailable semantics;
- PostgreSQL write/read;
- browser;
- counts/dates/coverage;
- детализацию долг/пени/штрафы там, где источник её предоставляет;
- отсутствие ложного clean-result при сбое источника.

### ФНС налоговые правонарушения

Статус: ✅ **ACCEPTED / SIX GATES PASS** 17.09.2026.

Протокол: `docs/STAGE_1_5_A2_TAX_OFFENCE_ACCEPTANCE.md`.

Подтверждены официальный snapshot ФНС `7707329152-taxoffence`, SHA-256, parser fail-closed, 24 139 PostgreSQL документов/компаний, сумма штрафа и даты, real found, dated not_found, IP not_applicable, unavailable contracts, Chromium и coverage. Тип нарушения источник не публикует и продукт его не выдумывает. Auto-update остаётся `NOT_CONFIGURED`.

Остальные ранее подключённые блоки: ФНС уплаченные налоги, REVEXP доходы/расходы, среднесписочная численность, Реестр МСП.

## FSSP / Fedresurs inventory rule

Владелец продукта указывает, что ФССП и Федресурс уже разрабатывались ранее.

До новой реализации Stage 1.5 обязан:

1. проверить текущий `main`;
2. проверить исторические ветки/коммиты;
3. проверить локальный проект/БД при необходимости;
4. найти models/migrations/providers/services/tests/UI;
5. классифицировать результат `READY / PARTIAL / LEGACY_MIGRATION / NOT_FOUND`;
6. переиспользовать рабочий код/данные и не дублировать интеграцию.

FSSP inventory result: `NOT_FOUND` on 17.09.2026. Evidence is recorded in `FSSP_STAGE_1_5_INVENTORY.md`. The disabled `fssp` / `fssp_enforcement` catalog rows are placeholders, not a working integration. No new FSSP implementation was added during the inventory step.

Fedresurs/EFRSB inventory result: `NOT_FOUND` on 17.09.2026. Evidence is recorded in `FEDRESURS_STAGE_1_5_INVENTORY.md`. The disabled catalog rows and planning specification are not a working integration. No new Fedresurs/EFRSB implementation was added during the inventory step.

## Постоянные критерии приёмки результата

Для каждого источника отдельно подтверждаем:

1. Автоматические тесты.
2. Реальную компанию с найденными сведениями из официального/допустимого публичного источника.
3. Сохранение и повторное чтение PostgreSQL.
4. Отсутствие записи, неприменимость и ошибку источника без ложного «всё хорошо».
5. Открытие реальной карточки в настоящем браузере.
6. Количество записей, даты и покрытие/полноту проверки.

Для каждого пункта: подтверждено / не подтверждено / неприменимо с причиной.
Код готов, данные загружены, карточка проверена и автообновление настроено — разные статусы.

## Person

Person остаётся отдельным последующим продуктовым направлением и не меняет текущий порядок по компаниям. Backlog и режимы доступа находятся в `PERSON_CHECK_SOURCES.md`.

До Privacy & Person Gate:

- не делать массовую SEO-индексацию физлиц;
- не публиковать избыточные персональные идентификаторы;
- не делать негативный вывод только по совпадению ФИО;
- учитывать expiry/purge источников;
- consent-only/document checks держать приватными.

## Важные продуктовые ограничения

- Не строить общий итоговый «индекс надёжности» как единственную истину без отдельного решения.
- Показывать факты, проверки, источники, даты, `Полноту проверки` и объяснение результата.
- `unavailable`/ошибка источника нельзя превращать в «ничего не найдено».
- Отсутствие записи относится только к конкретному набору/дате/проверке.
- Судебные требования не равны подтверждённому долгу.
- Ликвидация не равна банкротству.
- Платный источник не становится обязательным без отдельного решения; free/public/official first.
- Checkpoint 17.09.2026: migration `c3d4e5f6a7b8` adds C1/C2 dated caches and common protected-source sessions. C2 official Moscow adapter has a real three-case AVTOVAZ result in PostgreSQL. C1 Checko bridge is code-ready but `ACCESS_PENDING` without a free key. E has a real official 10-row positive browser result and confirmed requester-BIK semantics; end-to-end browser resume remains pending. F stores the real `challenge_required` state; no CAPTCHA was solved. Stage 1.5 remains `IN PROGRESS`.
- Новый источник не добавляется в Wave 2 обязательный scope автоматически.

# W1-006 — НОСТРОЙ / НОПРИЗ / СРО: паспорт источников

Статус: **ACCEPTED / SIX GATES PASS на пользовательском Mac**
Дата исследования и приёмки: 17.09.2026

## Scope и модель публикации

W1-006 включает: (A) реестр членов строительных СРО НОСТРОЙ; (B) реестр
членов СРО НОПРИЗ; (C) НРС НОПРИЗ в закрытом person-контуре; (D) безопасный
адаптер состояния для НРС НОСТРОЙ без обхода защиты; (E) обзор других
официальных СРО-реестров как отдельного backlog.

Company projection содержит только точные идентификаторы организации,
членство/статус, даты, номер записи и СРО. Person records по умолчанию
`PRIVATE_INTERNAL`: они не попадают в публичную карточку, API, поиск, SEO или
массовые выгрузки. Домашний адрес, личные телефон/email, паспорт, СНИЛС и
другие ненужные чувствительные идентификаторы не сохраняются.

| Entity/field | Категория |
|---|---|
| Company exact INN, member/SRO IDs, status and dates | PUBLIC, с датой и оговоркой источника |
| Person name, official specialist record/registration number, professional status | PRIVATE_INTERNAL |
| Закрытый кабинет Person/Compliance | USER_AUTHENTICATED_ONLY, только после отдельного legal/product gate |
| Контакты, адрес проживания, паспорт, СНИЛС и неиспользуемые raw-поля | EXCLUDE |

## A. НОСТРОЙ — члены строительных СРО

- владелец: Ассоциация «Национальное объединение строителей»;
- официальный реестр: `https://reestr.nostroy.ru/sro/all/member/list`;
- machine channel: undocumented JSON `POST https://reestr.nostroy.ru/api/sro/all/member/list`;
- формат/режим: JSON, low-load exact-INN on-demand с датированным кэшем;
- идентификаторы: exact 10-digit INN; OGRN как подтверждение; member id,
  регистрационный/inventory номер, SRO id и регистрационный номер СРО;
- наблюдаемый общий count: `419695` строк (текущие и исторические записи), не
  число активных компаний и не подтверждение внутренней полноты;
- real found: INN `5907056036`, member `4047586`, OGRN `1135907001801`, статус
  «Исключен», SRO `СРО-С-171-13012010`;
- real not_found: INN `9102309919`, только после успешного HTTP/JSON `count=0`;
- first-page SHA-256: `ad4c0bd7c3897f456ee2dbb27b70345cecdafade65228becaad9f71a7d1b41b5`;
- found SHA-256: `ace74fc6bf4df0096187dce750c40e1669fd9d40e66371df0d5a7b3757c55b4f`;
- not-found SHA-256: `59df9b9f68b1e6bcd3ca2cf769af42d1dc3b05ead8feb8f8fe50aefd919bae7f`.

Регламент: `https://nostroy.ru/dokumenty/reglament_er.pdf`. Частота обновления,
версионированный API contract, SLA, rate limits и права на массовое
коммерческое копирование не опубликованы. Статус: **ACCESS / LEGAL REVIEW
REQUIRED** для bulk/republication; текущий безопасный режим — on-demand.

## B. НОПРИЗ — члены СРО

- владелец: Национальное объединение изыскателей и проектировщиков;
- официальный реестр: `https://reestr.nopriz.ru/sro/all/member/list`;
- machine channel: undocumented JSON `POST https://reestr.nopriz.ru/api/sro/all/member/list`;
- формат/режим: JSON, low-load exact-INN on-demand с кэшем/backoff;
- наблюдаемый общий count: `212903`, `10646` страниц;
- first-page SHA-256: `08888250c7bf56c98dcd16faa6a605e50b2467d1df544dd060b30ed09574a68e`;
- real found: INN `7810978295`, одна active запись, member id `19529862`, SRO
  `СРО-П-031-28092009`;
- found SHA-256: `193cae3023d51fdfd6cba82102b8bbea61f403aa3707225ccae2d1a6b7f0eafb`;
- проверка `9102309919` завершилась timeout после 120 секунд и корректно
  сохранена как `unavailable`, а не `not_found`.

Условия automation/storage/reuse, SLA и rate limits отдельно не опубликованы:
**ACCESS / LEGAL REVIEW REQUIRED**. Bulk crawl не выполнялся.

## C. НРС НОПРИЗ — private person source

- официальный реестр: `https://nrs.nopriz.ru/`;
- machine channel: undocumented JSON `POST https://nrs.nopriz.ru/api/specialist/list`;
- наблюдаемый count: `174395`; first-page SHA-256:
  `3116bf8f5ec85a80fcc574c6e4368f17b35ce2fd2dc971079772ca8e2405ea97`;
- проверен exact официальный регистрационный номер `П-000011`, `count=1`;
  response SHA-256:
  `54dd5a57fb8d6cf51713f2f695a68c1c7f8732bc86acd1ada761d67d5cafa3ea`;
- в PostgreSQL сохранена одна минимизированная запись, `public_visibility=false`;
  связь с Company источником не опубликована и помечена
  `manual_review_required`.

Подтверждение личности разрешено только по official record id/registration
number или иному устойчивому официальному идентификатору. ФИО без такого
идентификатора не даёт `confirmed`; используются `possible_match` или
`manual_review_required`. Person bulk/republication не разрешены без отдельного
review.

## D. НРС НОСТРОЙ

- официальный URL: `https://nrs.nostroy.ru/`;
- наблюдаемый server-side count: `321809`;
- идентификаторы, ФИО и даты выдаются как защищённые изображения.

OCR, декодирование или обход защиты не выполнялись. Адаптер возвращает
`unavailable/source_protection`. Публичный просмотр заявлен бесплатным, но
automation/storage/reuse для machine integration неясны: **ACCESS / LEGAL
REVIEW REQUIRED**.

## E. Другие официальные СРО-реестры

Реестры отдельных СРО и ведомственные профессиональные реестры рассмотрены.
Fedresurs/реестр арбитражных управляющих и похожие источники относятся к
другим продуктовым доменам и контрактам доступа. Они остаются отдельным
backlog и не смешиваются с construction/design SRO без собственного паспорта,
legal gate и six-gate acceptance.

## Семантика и coverage

- `found`: успешный exact-identifier ответ содержит согласованные записи;
- `not_found`: только успешный полный ответ с согласованным `count=0`;
- `not_applicable`: тип лица/контекст не относится к проверке;
- `unavailable`: запрос не выполнялся, timeout/HTTP/protection, schema/count или
  identity mismatch. Ошибка, captcha или неполный ответ никогда не означают
  отсутствие.

На 17.09.2026 Master Registry содержит `6781487` сущностей. Company cache:
2 NOSTROY (`1 found`, `1 not_found`) и 2 NOPRIZ (`1 found`, `1 unavailable`),
`3` уникальных checked INN, все 3 совпали с Master; `6781484` Master entities
не проверены. Private person storage: `1`; linked/confirmed company
relationships: `0`; manual review: `1`; public person records: `0`. Bulk
completeness: **N/A** — cache не является снимком реестра.

## Эксплуатационное решение

Разрешён только low-load exact lookup с cache reuse, request budget и backoff.
Auto-update: `NOT_CONFIGURED`. Массовый crawl и публичная перепубликация не
разрешены до подтверждения условий. Source counts и record dates сохраняются
отдельно от `checked_at`; число строк не трактуется как число активных компаний
или уникальных лиц.

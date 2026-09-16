# W1-004 — финальная локальная приёмка

**ACCEPTED / W1-004 SIX GATES: PASS на пользовательском Mac.**

Дата: 16.09.2026. Scope: четыре утверждённых официальных канала
Росздравнадзора — A (три bulk-реестра лицензий), B (точечный поиск Единого
реестра лицензий), C (lookup медизделия по точному номеру РУ), D (bulk-перечень
организаций для клинических исследований медизделий).

Code baseline до изменений: `7d8589c9a579768ec33929a5c5f43668d75e0635`.
Локальный отчёт:
`data/acceptance/w1-004/final/report.json`.

- `platform=Darwin`;
- PostgreSQL `kontragent`;
- Alembic `a9c4e6f8b201 (head)`;
- `450 passed`;
- gates 1–6: `PASS`;
- `accepted=true`, `user_mac_accepted=true`;
- `auto_update=NOT_CONFIGURED`.

## Gate 1 — автоматические тесты

PASS: профильные provider/parser/service/template тесты и полный regression
suite. W1-001—W1-003 не переимпортировались.

## Gate 2 — реальные официальные данные

PASS. A и D загружены из официальных файлов Росздравнадзора с проверкой
ZIP CRC/EOF, полного состава и SHA-256. B подтверждён реальными HTTP 200:
ИНН `1658122916` — `found`, 3 записи; ИНН `9102309919` — `not_found`, 0 строк.
C подтверждён реальными HTTP 200: РУ `ФС 012а2004/0344-04` — `found`, 1 запись;
`W1-004-NOT-FOUND` — `not_found`, 0 строк.

ELK использует Russian Trusted Root CA, отсутствующий в стандартном certifi.
Верификация TLS не отключена: клиент C использует фиксированный официальный
trust anchor с SHA-256 fingerprint
`D26D2D0231B7C39F92CC738512BA54103519E4405D68B5BD703E9788CA8ECF31`
только для фиксированного ELK endpoint.

## Gate 3 — PostgreSQL write/read

PASS. Bulk snapshots, ingestion ledger и датированные B/C cache rows записаны
в пользовательскую PostgreSQL и независимо перечитаны. Ошибки источника не
превращаются в `not_found`; публикация bulk snapshot атомарна.

## Gate 4 — семантика

PASS. Проверены `found`, `not_found`, `unavailable` (включая `not_checked`) и
`not_applicable`. Для C связь с Company имеет `not_applicable`, потому что
официальный ответ не содержит ИНН/ОГРН производителя; fuzzy matching по имени,
адресу или сайту запрещён. `found`/`not_found` C относятся только к exact lookup
по номеру РУ.

## Gate 5 — Chromium

PASS, Chromium `153.0.8010.12`:

- ИНН `1658122916`, ООО «АКТИМЕД»: A+B `found`, HTTP 200, `page_errors=[]`;
- ИНН `9102309919`, АО «КРЫМАВТОДОР»: A+B `not_found`, HTTP 200,
  `page_errors=[]`;
- ИНН `0275080530`, ООО «ИГИТИ»: D `found`, HTTP 200, `page_errors=[]`.

Скриншоты блока Росздравнадзора:
`data/acceptance/w1-004/final/license-found.png`,
`license-not-found.png`, `clinical-found.png`.

## Gate 6 — количества, даты и покрытие

Master Registry: **6 781 485** компаний/ИП.

| Dataset | Исходных / сохранённых | Уникальных ИНН | Exact-INN Master | Дата | Дубли / rejected | SHA-256 |
|---|---:|---:|---:|---|---:|---|
| Фармацевтические лицензии | 21 433 / 21 433 | 21 433 | 14 644 | 13.09.2026 | 0 / 0 | `57cd27a979c7f5e91a52f15033a80ee524ed9ba1f965ea5d643aad6b180ee468` |
| Наркотические/психотропные лицензии | 7 429 / 7 429 | 7 429 | 1 935 | 13.09.2026 | 0 / 0 | `266bc0e09ca46a14f4c67b0b40c63d95cb905e24c5c6bfd3250d22278f3ef8b3` |
| Обслуживание медизделий | 2 699 / 2 699 | 2 698 | 2 423 | 13.09.2026 | 0 / 0 | `cc58a7675680512ecb27b46715fb7f7ed663f0408131afb223ce366785d0bd74` |
| Клинические организации | 295 / 294 | 291 | 45 | 13.09.2026 | 1 / 0 | `9db1ab2eec6b69f83cdb453806cc61e9244375e2378eb60d7771e484d2eb12e5` |

В D одна полностью идентичная строка дедуплицирована и отражена в ledger.
У A имя файла/дата изменения — 13.09.2026, а metadata «дата актуальности» —
20.09.2026 (будущая относительно проверки); значения не смешиваются.

B — on-demand cache, а не bulk: на дату acceptance 3 успешных cache-INN,
2 связаны с Master Registry; checked Master 2, unchecked 6 781 483. C — 2
успешных exact-number cache rows; Master coverage N/A из-за отсутствия
официального company identifier.

Ручная загрузка и точечные проверки не означают расписание:
`auto_update=NOT_CONFIGURED`.

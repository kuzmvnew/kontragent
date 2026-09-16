# W1-005 — Роскомнадзор: промежуточная приёмка

Дата: 17.09.2026

Итог: **IN PROGRESS / SOURCE-BLOCKED (B, C)**.

Scope A–F реализован. Нельзя ставить `SIX GATES PASS`, пока два обязательных
официальных bulk snapshot не получены полностью и не прошли PostgreSQL
write/read. Частичные файлы не загружались.

## Gates

| Gate | Состояние | Доказательство |
|---|---|---|
| 1. Tests | PASS | `458 passed in 1.01s` |
| 2. Реальные официальные данные | PARTIAL | A, D, E и F подтверждены; B/C premature EOF |
| 3. PostgreSQL write/read | PARTIAL | Alembic `c5e1a2b3d4f5`; A/D/E/F прочитаны обратно |
| 4. Семантика статусов | PASS | found/not_found/not_applicable/unavailable разделены; B/C=`unavailable` |
| 5. Chromium | PASS для текущего состояния | HTTP 200; A=`found`, B/C=`unavailable`; блок видим |
| 6. Counts/dates/coverage | PARTIAL | A/D/E и F посчитаны; B/C отсутствуют |

## PostgreSQL readback

| Канал | Дата | Строк источника | Публичные ЮЛ | Закрытые | Exact-INN Master matches | SHA-256 |
|---|---:|---:|---:|---:|---:|---|
| A — связь | 2026-09-15 | 195 681 | 182 555 | 13 126 | 11 803 | `02ec5e50504f858fac41506d61865b23006a95655d8543ccbec4757089ee2bb3` |
| B — вещание | — | — | — | — | — | официальный response обрывается |
| C — СМИ | — | — | — | — | — | официальный response обрывается |
| D — ORI | 2026-09-16 (дата получения) | 475 | 282 | 193 | 166 | `9dae6e7e935eaba04d06eac355031f33dd6743ec3ac54eba839ce9e340fe0655` |
| E — хостинг | 2026-07-30 | 595 | 496 | 99 | 399 | `97d0964bf3ea7bf455afedba1ffa3554840a1ca8d3b3580218adcea4764e1a80` |
| F — операторы ПДн | 2026-09-16 | on-demand | found 1 / not_found 1 | 0 опубликовано | cache по ИНН | N/A |

Закрытые строки находятся в `roskomnadzor_private_person_records`, всегда
имеют `is_published=false` и не читаются product service/aggregator/template.
Публичные факты не содержат ФИО контактных лиц, телефоны или e-mail.

## Реальная семантика

- A, ИНН `6320002223` (АО «АВТОВАЗ»): `found`, 23 записи по exact ИНН.
- F, ИНН `9706063520`: HTTP 200, `found`, реестровый номер
  `77-26-562985`; после записи получен из PostgreSQL cache.
- F, ИНН `9102309919`: HTTP 200, `not_found`; ошибка не использовалась как
  отсутствие.
- B/C до полного snapshot: `unavailable / dataset_not_loaded`.
- Для 12-значного ИНН company-проверка: `not_applicable`; person-продукт не
  запускался.

## Дефект официальной раздачи B/C

Официальные ссылки и страницы открываются. Большие XML многократно
обрываются до закрывающего тега реестра. Наблюдения:

- `curl` HTTP/1.1: `transfer closed with outstanding read data remaining`;
- `httpx`: `peer closed connection ... incomplete chunked read`;
- HTTP Range проигнорирован: сервер вернул `200 OK`, `Transfer-Encoding:
  chunked`, а не `206 Partial Content`;
- HTTP/1.0 завершился кодом 0 из-за отсутствия chunk framing, но обязательный
  XML parser обнаружил `unclosed token`, поэтому файл был отклонён;
- браузерная загрузка той же официальной ссылки не завершилась;
- локальное восстановление closing tags, склейка повторов, неофициальные
  зеркала и устаревшие копии не использовались.

Следующий допустимый шаг: повторить загрузку B/C с официального сайта после
восстановления стабильной раздачи, затем выполнить
`scripts.sync_roskomnadzor`, полный `scripts.accept_roskomnadzor --tests --browser`
и отдельно пересмотреть итоговый статус после всех шести PASS. Повторная
приёмка B/C не требует повторной реализации или перезагрузки A/D/E/F.

Автообновление: `NOT_CONFIGURED`.

# FNS account-suspension decisions — Stage 1.5 access decision

Date: 2026-09-17

Status: **SOURCE_BLOCKED FOR UNATTENDED MACHINE ACCESS**

## Official source

- Service: FNS “Система информирования банков о состоянии обработки электронных документов (БАНКИНФОРМ)”.
- Public URL: <https://service.nalog.ru/bi.do>.
- Current FNS explanation: <https://www.nalog.gov.ru/rn27/news/activities_fts/16628104/>.
- The official page states that any taxpayer can query their own or a counterparty's current suspension decisions using INN and a bank BIK.

The FNS response can contain decision number/date, start date, basis code, FNS office code, the BIK to which the decision was sent, publication timestamp, and, for some basis codes, negative unified-tax-account balance. These are current suspension **decisions**, not a complete list of every account or every restriction imposed by other authorities.

## Technical probe

All probes were low-load and used the ordinary public UI/endpoint. No CAPTCHA was solved or bypassed.

1. Ordinary Chromium loaded service version 3.51.2 and selected “Запрос о действующих приостановлениях операций по счетам”.
2. Query `ИНН 6320002223` / `БИК 044525225` succeeded at `17.09.2026 10:07:54 МСК` and returned: “Действующие приостановления ... ОТСУТСТВУЮТ”. This is a real dated official `not_found` for the scope of this FNS check.
3. The required control query for the same INN with another valid BIK, `044525593`, immediately opened the official CAPTCHA dialog. It was not completed.
4. The public HTML posts `requestType=FINDPRS`, `innPRS`, `bikPRS`, and CAPTCHA fields to `bi2-proc.json`.
5. One direct POST with empty CAPTCHA fields returned HTTP 400 and a structured error: `captcha: Требуется ввести цифры с картинки`.

## BIK semantics

The form labels the supplied value as “БИК банка, выполняющего запрос”. A positive response separately lists the BIK of the bank where operations are suspended. Accordingly, the request BIK is a required requester-context field, not a reliable filter proving that the taxpayer has an account at that bank. The multi-BIK experiment could not be completed because the second request activated CAPTCHA.

## Product semantics

- Positive result: active FNS suspension decision, with source fields and query timestamp.
- Negative result: only “no active FNS decisions returned for this INN by this dated query”.
- CAPTCHA, HTTP error, timeout, schema error, or incomplete response: `unavailable`, never `not_found`.
- Do not claim all bank accounts are unblocked: FSSP and other restrictions are outside this source, and FNS itself directs users to FSSP when relevant.
- A displayed negative result must disclose the query date/time and this coverage boundary.

## Six Gates

| Gate | Result |
|---|---|
| Automated tests | Not applicable: no production-safe adapter |
| Real found | Not confirmed; further requests require CAPTCHA |
| PostgreSQL write/read | Not applicable |
| State semantics | Real dated `not_found` observed; CAPTCHA/direct access correctly classified `unavailable` |
| Real Chromium | Confirmed |
| Counts/dates/coverage | Single official negative response confirmed; multi-BIK and positive coverage not confirmed |

## Decision

The official service is useful for manual verification but is not presently a stable unattended machine path. An adapter that relies on CAPTCHA solving, session cycling, stealth, proxy rotation, or fingerprint evasion is prohibited and was not implemented. E remains open as `SOURCE_BLOCKED FOR UNATTENDED MACHINE ACCESS`.

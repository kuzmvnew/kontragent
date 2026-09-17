# FNS account-suspension decisions — Stage 1.5 access decision

Date: 2026-09-17

Status: **HUMAN-ASSISTED PRODUCT FLOW PASS**

## Final live product acceptance

A new ordinary visible-browser query for master-registry company AVTOVAZ (`6320002223`) with requester BIK `044525161` completed without challenge. At `17.09.2026 13:42:10 МСК`, the official result stated that current suspension decisions are absent. The result was parsed as `active_suspensions_not_found`, persisted with source scope/evidence hash/browser metadata, and is cache-readable by the company card.

This does not replace the earlier positive golden-company evidence and does not claim all accounts or non-FNS restrictions are absent.

## Superseding BIK experiment and implementation checkpoint

A second golden-company experiment supersedes the earlier “real found not confirmed” conclusion:

- INN `7702059544`, requester BIK `044525161` (LOCO-Bank), ordinary visible browser;
- official result at `17.09.2026 11:38:29 МСК`: 10 active suspension rows for АО «ЦЕНТРОДОРСТРОЙ»;
- returned decision BIKs included `044525225`, `044525411`, `044525593`, `044525666`, `044525823` — not the requester BIK;
- observed start date `16.06.2025`, decision numbers including `14674761` and `24115347`, decision dates `14.09.2026` and `16.09.2026`, basis `01`, tax office `7702`, and negative ENS balances;
- the next query with another real BIK opened the official rate-limit CAPTCHA. It was not solved or bypassed.

This is direct evidence that the entered BIK is requester context, not a filter restricting the returned decisions to that bank. The first response itself returned five other BIKs. Additional automated repetitions would add load and are neither required nor allowed through the challenge.

`InteractiveProtectedSourceSession` now provides the common FNS/ZSK lifecycle: create, visible-browser mode, `challenge_required`, same-session resume requirement, evidence hash/metadata, completed result, close, and cache. The card creates a session only after a user click and otherwise makes zero requests. Result parsing is fail-closed. The golden company is not in the current master registry, so its research evidence was documented but not forced into a company FK; production sessions are stored for master-registry companies.

Checko `/company` public API documentation contains no published suspension field. Checko HTML is therefore not used as a production adapter.

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
| Automated tests | PASS for challenge/unavailable fail-closed parser and session result constraints |
| Real found | PASS in official visible browser: 10 active rows for INN 7702059544 |
| PostgreSQL write/read | Foundation schema PASS; golden evidence not inserted because the company is absent from master registry |
| State semantics | Real dated `not_found` observed; CAPTCHA/direct access correctly classified `unavailable` |
| Real Chromium | Confirmed |
| Counts/dates/coverage | Single official negative response confirmed; multi-BIK and positive coverage not confirmed |

## Decision

E is **HUMAN-ASSISTED PRODUCT FLOW PASS**: positive semantics, dated negative semantics, PostgreSQL cache and product projection are all evidenced. If CAPTCHA appears later, the framework transitions to `waiting_for_user` and must resume the same session after manual completion.

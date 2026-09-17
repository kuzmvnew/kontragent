# Courts of General Jurisdiction v1 — access decision

Date: 2026-09-17

Status: **FREE OFFICIAL FOUNDATION WORKING / PARTIAL NATIONAL COVERAGE**

## Superseding implementation checkpoint

The earlier blocked conclusion below is superseded by deeper browser/form research:

1. The official Moscow portal `https://mos-gorsud.ru/search` supports a low-load GET query by participant/organization, pagination and court/category/case filters. A real query for `АКЦИОНЕРНОЕ ОБЩЕСТВО "АВТОВАЗ"` returned three official case rows and stable official detail links.
2. The shared regional `sudrf` form exposes exact `ИНН`, `КПП`, `ОГРН`, and `ОГРНИП` fields. The confirmed common GET keys include `G2_PARTS__INN_STRSS` and `G2_PARTS__OGRN_STRSS`. This form exists across district, regional, appellate and cassation court sites.
3. A real exact-INN query for AVTOVAZ on the First Appellate Court site returned the honest court-local negative `Данных по запросу не обнаружено`; it is not a nationwide `not_found`.
4. Central BSR still timed out, so it remains unavailable rather than negative. SudAct remains discovery-only and must be corroborated by an official page.

Implemented adapters/contracts:

- `GeneralCourtProvider`;
- `MoscowCourtProvider` — working official adapter;
- `RegionalSudrfProvider` — shared exact-identifier request contract, deliberately one configured court at a time;
- `GasPravosudieProvider` — access-status adapter pending central confirmation;
- `SudactDiscoveryProvider` — non-authoritative fallback marker.

The Moscow parser keeps case number, court, category, role, stage/current state, result, parties, official URL, matching method, confidence, and evidence. Exact full legal-name matches are `medium`, not `high`, because the Moscow list does not show INN. Name-only discoveries are excluded from confirmed cases.

PostgreSQL migration `c3d4e5f6a7b8` stores dated checks, cases, coverage and failures. The AVTOVAZ browser response was parsed into three cases and written/read from PostgreSQL. The company card is cache-only until the user clicks “Проверить суды общей юрисдикции”. Coverage is conservatively labelled `MOSCOW TARGETED QUERY (FIRST 100) / OTHER REGIONS NOT CHECKED`.

## Official surfaces reviewed

- Portal of GAS “Pravosudie”: <https://sudrf.ru/>.
- Official federal-court directory and link to the cross-court search: <https://sudrf.ru/index.php?id=300>.
- Cross-court case/act search target: <https://bsr.sudrf.ru/bigs/portal.html>.
- Official court-site instructions explaining the public search and publication limits, for example: <https://vologodskygor--vld.sudrf.ru/modules.php?name=information&rid=7>.
- Official IAC description of the consolidated/depersonalised decision bank: <https://iac.cdep.ru/%D1%85%D1%80%D0%B0%D0%BD%D0%B5%D0%BD%D0%B8%D0%B5-%D0%B8-%D0%B0%D0%B2%D1%82%D0%BE%D0%BC%D0%B0%D1%82%D0%B8%D0%B7%D0%B8%D1%80%D0%BE%D0%B2%D0%B0%D0%BD%D0%BD%D0%B0%D1%8F-%D1%81%D1%83%D0%B4%D0%B5%D0%B1%D0%BD%D0%BE%D0%B9-%D0%B8%D0%BD%D1%84%D0%BE%D1%80%D0%BC%D0%B0%D1%86%D0%B8%D0%B8/>.

## Findings

1. The official portal is reachable and publishes a human-facing directory of federal courts. In ordinary Chromium on 2026-09-17 it exposed the official “Поиск по делам и текстам судебных актов” link.
2. The linked central BSR search timed out in the same ordinary browser. No bypass or repeated load was attempted.
3. The first documentation pass described search by case number/name only. Direct inspection of the current shared court form later confirmed exact INN/OGRN fields and their GET keys; the central documentation remains incomplete.
4. Individual court sites publish case movement and some depersonalised acts, but coverage is distributed across courts and case types. Some acts are absent because they are not yet published or are excluded by law.
5. The official IAC describes a consolidated bank and public depersonalised decisions, but no documented public company-search API, feed, key process, machine-use limits, or stable bulk export was found.
6. The authenticated `ej.sudrf.ru` service is for participants and requires ESIA/qualified signature; it is not a public counterparty-search API.

## Product consequence

The available official public interfaces cannot support an honest nationwide company result:

- names are not stable exact identifiers and create material false-positive/false-negative risk;
- INN/OGRN search exists on inspected regional forms but central/national orchestration and completeness are not documented;
- distributed/depersonalised publication cannot establish completeness;
- central search availability is currently unconfirmed;
- source outage or missing publication cannot become `not_found`.

Therefore a C2 foundation is implemented, but no nationwide “clean” company result is emitted. A source failure is `unavailable`; a court-local empty result is scoped to that portal/catalogue only.

## Six Gates

| Gate | Result |
|---|---|
| Automated tests | PASS: parser, matching boundary and exact sudrf parameter contract |
| Real official found | PASS: 3 Moscow official cases for AVTOVAZ by exact full legal name; confidence medium |
| PostgreSQL write/read | PASS: 3 cases and coverage cached |
| Correct state semantics | Decision fixed: unavailable, never inferred not_found |
| Real Chromium | Portal loaded; central BSR target timed out |
| Counts/dates/coverage | PASS for the stored query: 3; first 100 Moscow results only, other regions not checked |

## Decision

C2 is **FOUNDATION PASS / PARTIAL NATIONAL COVERAGE**. It is not nationwide acceptance: central BSR and safe orchestration across selected regional portals remain follow-up work. Kontur Focus `/api3/generalCourtCases`, Casebook and SPARK remain future paid candidates only.

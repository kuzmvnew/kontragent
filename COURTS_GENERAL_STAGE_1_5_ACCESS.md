# Courts of General Jurisdiction v1 — access decision

Date: 2026-09-17

Status: **TECHNICAL_ACCESS_UNCONFIRMED / SOURCE_BLOCKED FOR COMPANY MATCHING**

## Official surfaces reviewed

- Portal of GAS “Pravosudie”: <https://sudrf.ru/>.
- Official federal-court directory and link to the cross-court search: <https://sudrf.ru/index.php?id=300>.
- Cross-court case/act search target: <https://bsr.sudrf.ru/bigs/portal.html>.
- Official court-site instructions explaining the public search and publication limits, for example: <https://vologodskygor--vld.sudrf.ru/modules.php?name=information&rid=7>.
- Official IAC description of the consolidated/depersonalised decision bank: <https://iac.cdep.ru/%D1%85%D1%80%D0%B0%D0%BD%D0%B5%D0%BD%D0%B8%D0%B5-%D0%B8-%D0%B0%D0%B2%D1%82%D0%BE%D0%BC%D0%B0%D1%82%D0%B8%D0%B7%D0%B8%D1%80%D0%BE%D0%B2%D0%B0%D0%BD%D0%BD%D0%B0%D1%8F-%D1%81%D1%83%D0%B4%D0%B5%D0%B1%D0%BD%D0%BE%D0%B9-%D0%B8%D0%BD%D1%84%D0%BE%D1%80%D0%BC%D0%B0%D1%86%D0%B8%D0%B8/>.

## Findings

1. The official portal is reachable and publishes a human-facing directory of federal courts. In ordinary Chromium on 2026-09-17 it exposed the official “Поиск по делам и текстам судебных актов” link.
2. The linked central BSR search timed out in the same ordinary browser. No bypass or repeated load was attempted.
3. Official instructions describe search by **case number and/or party surname/name**, optionally narrowed by region and court. They do not document exact company matching by INN or OGRN.
4. Individual court sites publish case movement and some depersonalised acts, but coverage is distributed across courts and case types. Some acts are absent because they are not yet published or are excluded by law.
5. The official IAC describes a consolidated bank and public depersonalised decisions, but no documented public company-search API, feed, key process, machine-use limits, or stable bulk export was found.
6. The authenticated `ej.sudrf.ru` service is for participants and requires ESIA/qualified signature; it is not a public counterparty-search API.

## Product consequence

The available official public interfaces cannot support an honest nationwide company result:

- names are not stable exact identifiers and create material false-positive/false-negative risk;
- INN/OGRN search is not documented;
- distributed/depersonalised publication cannot establish completeness;
- central search availability is currently unconfirmed;
- source outage or missing publication cannot become `not_found`.

Therefore no C2 adapter was implemented and no “clean” company result is emitted. Any future provider must return `unavailable` until a documented exact-identifier route and coverage contract are proven.

## Six Gates

| Gate | Result |
|---|---|
| Automated tests | Not applicable: no safe adapter |
| Real official found | Not confirmed for exact company identity |
| PostgreSQL write/read | Not applicable |
| Correct state semantics | Decision fixed: unavailable, never inferred not_found |
| Real Chromium | Portal loaded; central BSR target timed out |
| Counts/dates/coverage | Not available as a nationwide exact-company audit |

## Decision

C2 remains open as `TECHNICAL_ACCESS_UNCONFIRMED`. A commercial aggregator may be researched under a separate access/cost decision, but is not approved here. The Stage 1.5 implementation continues with E/F/G/H without claiming C2 completion.

# Evidence Reporting Policy

Date: 2026-09-17
Status: ACTIVE GOVERNANCE RULE

## Rule

Do not report `проверено`, `готово`, `создано`, `загружено`, `подключено`, `тесты прошли`, `browser PASS`, `данные актуальны`, `источник работает`, `PR создан` or `файл существует` without evidence produced by the corresponding system or tool.

Code presence, loaded data, PostgreSQL verification, browser acceptance, deployed scheduling and current source availability are separate claims. Evidence for one does not prove another. `unavailable` must never be converted to `not_found`.

## Required evidence classes

- `VERIFIED_FROM_GIT`: directly confirmed from the named tracked file, commit, tree, branch or PR metadata. Cite the path/SHA/URL.
- `VERIFIED_RUNTIME`: directly observed in the named command, test, database read, HTTP/browser run or deployed system. Record command/environment/date and exact result.
- `USER_REPORTED`: stated by the owner but not independently reproduced in the current run.
- `EXTERNAL_HISTORICAL_EVIDENCE`: preserved evidence from another system or dated audit that the current run cannot independently inspect.
- `INFERENCE`: a reasoned conclusion; state its premises and do not present it as an observation.
- `UNVERIFIED`: no sufficient evidence was available. Use `NOT PRESENT` only after a bounded search proves absence in the searched scope.
- `CONFIRMED_HALLUCINATION / FALSE_COMPLETION_REPORT`: a forensic classification for a prior completion claim contradicted by, or lacking, the required tool artifact/evidence. It classifies the report, not application-code integrity.

## Minimum recording

Every operational acceptance or completion claim records:

1. evidence class;
2. exact artifact, command, query, URL or tool output;
3. date and relevant environment;
4. observed result and limitations;
5. what was not checked.

Historical acceptance evidence remains historical evidence and is not deleted merely because a newer run exists. A new run must report its own result without silently rewriting the historical checkpoint.

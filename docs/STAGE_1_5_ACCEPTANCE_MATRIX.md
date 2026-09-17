# Stage 1.5 acceptance matrix

Date: 2026-09-17

Stage status: **ACTIVE / NOT CLOSED**

Final regression: **531 tests passed**. Alembic: `c3d4e5f6a7b8 (head)`. Master Registry: **6,781,487 companies**.

| Workstream | Status | Tests | Real source | PostgreSQL | Semantics | Chromium | Coverage / access | Remaining limitation |
|---|---|---|---|---|---|---|---|---|
| A1 Tax Debt | PASS / SIX GATES | PASS | Official FNS snapshot | PASS | dated found/not_found/not_applicable/unavailable | PASS | accepted dataset | none for Stage 1.5; auto-update is later |
| A2 Tax Offences | PASS / SIX GATES | PASS | Official FNS snapshot | PASS | no invented offence type | PASS | accepted dataset | auto-update is later |
| B1 FSSP | INVENTORY COMPLETE / NOT_FOUND | inventory regression | no implementation found | catalog placeholder only; no tables/runs | no clean result fabricated | n/a | access research backlog | new implementation deferred |
| B2 Fedresurs/EFRSB | INVENTORY COMPLETE / NOT_FOUND | inventory regression | no implementation found | catalog placeholder only; no tables/runs | D events not synthesized from placeholder | n/a | access research backlog | new implementation deferred |
| C1 Arbitration | ACCESS_PENDING | adapter/pagination/signals PASS | Checko docs; live key unavailable to server process | access-pending cache PASS; golden exists in Master | sample/full separation; claim != debt | card PASS; live result pending | free bridge implemented | expose `CHECKO_API_KEY` to server process, then live Six Gates |
| C2 General Courts | ROUTING FOUNDATION / PARTIAL MULTI-REGION | Moscow parser + SPb/Sverdlovsk route contracts PASS | Moscow 3 real cases; official SPb/Sverdlovsk portals confirmed | Moscow cache PASS | exact identifier strong; exact legal name medium | Moscow card PASS | targeted Moscow + configured SPb/Sverdlovsk, not nationwide | regional live result parser/orchestration not yet Six Gates |
| D Bankruptcy/Liquidation | ACCEPTED FOUNDATION | PASS | source-independent evidence model | migration/table PASS | 15 states; liquidation != bankruptcy | compatible card/API | adapter coverage pending | no event source fabricated |
| E FNS Suspensions | HUMAN-ASSISTED PRODUCT FLOW PASS | state/parser PASS | positive golden result + dated AVTOVAZ negative | AVTOVAZ cache PASS | challenge/error != not_found; BIK semantics confirmed | official result PASS | current FNS decisions only | future challenges require manual same-session resume |
| F CBR ZSK | HUMAN-ASSISTED PRODUCT FLOW PASS | state/parser PASS | official dated AVTOVAZ negative | completed cache PASS | only high-risk presence; challenge/timeout != negative | official result PASS | point-in-time public high-risk information | future challenges require manual same-session resume |
| G PRIME Disclosure | PASS / SIX GATES | PASS | real PRIME exact-INN page | PASS | issuer disclosure only | PASS | accepted public foundation | none for Stage 1.5 |
| H Company Facts | COMPLETE FOUNDATION | vocabulary/hash/address tests PASS | PRIME + Master Registry | real AVTOVAZ facts PASS | missing facts never inferred | existing card compatible | schema complete; mappings partial | building/room index and additional lawful mappings later |

## Closure decision

Stage 1.5 is not closed because C1 lacks a live server-visible key probe and C2 regional routes are configured but not yet live accepted. E/F now pass. No merge is permitted.

The next stage, Auto-update / Data Readiness, must not start automatically.

# Courts Stage 1.5 Access Decision

Date: 2026-09-17

## C1 Arbitration Courts

Status: **SOURCE_BLOCKED / PRODUCT-OWNER DECISION REQUIRED**

### Official/public path tested

- Source of truth: official `https://kad.arbitr.ru/Kad` (Kartoteka arbitrazhnykh del).
- The official arbitr.ru inventory identifies KAD as an information system administered for the federal arbitration courts.
- Official public information confirms that public case movement and published judicial acts are available through KAD/Bank of Decisions, subject to statutory publication limits.

### Technical evidence

1. The public KAD page and its current public JavaScript were inspected at low load.
2. The browser workflow sends a JSON search request to the site's internal `SearchInstances` endpoint and explicitly supports a CAPTCHA/`RecaptchaToken` challenge.
3. One interactive Chromium query by exact INN `1215214540` succeeded on 2026-09-17 and displayed **54 cases**. This proves that the public human-facing lookup currently works and that real official case results exist.
4. One equivalent direct machine POST, with ordinary browser headers and the public page session, returned **HTTP 451 / access blocked** from the site's DDoS protection.
5. One standard headless Chromium attempt, with no stealth or protection bypass, loaded the page but produced no result rows within 90 seconds.
6. No documented official public bulk API or stable machine API for company case search was found on the official arbitration-court surfaces reviewed.

### Decision

The interactive browser result is valid real-source evidence, but it is not a production-safe server-side ingestion path. Building around stealth, CAPTCHA solving, proxy rotation, fingerprint evasion, or undocumented protection bypass is prohibited by Stage 1.5 and was not attempted.

Therefore the current free path cannot honestly satisfy the required working Arbitration Courts v1 provider. No code emits `not_found`; an automated attempt under these conditions must remain `unavailable`.

To continue C1, the product owner must authorize one of:

1. evaluation/procurement of a licensed machine feed or API; or
2. a reduced manual/operator-assisted import scope, explicitly acknowledging that it does not satisfy on-demand product automation and therefore does not close the current Stage 1.5 criterion.

The first option may introduce a paid source and requires a separate cost/access decision. No vendor or commercial aggregator is approved by this document.

## C2 Courts of General Jurisdiction

Status: **NOT STARTED**

Per the approved sequential work order, C2 research is paused until the C1 decision. No protection bypass or paid substitute has been attempted.

## Semantics retained

- A claim amount is not confirmed debt.
- Party role must be exact and source-backed.
- A source failure, CAPTCHA, HTTP 451, timeout, or incomplete page is `unavailable`, never `not_found`.
- No court outcome prediction is in scope.

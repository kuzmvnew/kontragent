# FINANCIAL DISTRESS SPEC — Kontragent

Status: APPROVED DESIGN
Date: 2026-09-17

## 1. Separate fact from forecast

Two independent concepts are mandatory.

### Bankruptcy Fact

An official source confirms a legal bankruptcy event/procedure/stage. This does not require a mathematical score.

Examples may include observation, external management, restructuring, bankruptcy proceedings, asset sale or another formally recorded stage according to the relevant subject type and law.

The product must display source, case/message identifier, date, current stage/status and freshness.

### Financial Distress Forecast

A statistical/analytical estimate that the company may face material financial distress or bankruptcy in a future horizon. This is a forecast, not a legal fact.

Never merge these two into one flag.

## 2. Core data inputs

The main future forecasting model should use lawful, verified data such as:

- GIR BO financial statements when connected/justified;
- existing FNS financial and tax datasets;
- enforcement facts;
- court exposure;
- Fedresurs/bankruptcy history and events where access is confirmed;
- other evidence-backed financial obligations;
- company age and relevant profile.

The exact active feature set depends on source availability and licensing.

## 3. Financial indicators

Candidate transparent metrics include:

- current liquidity;
- absolute/quick liquidity where the reporting structure supports it;
- equity / assets;
- liabilities / assets;
- short-term liabilities / current assets;
- debt / revenue;
- working capital;
- revenue trend;
- profit/loss trend;
- equity trend;
- accounts payable trend;
- cash-flow measures where available;
- negative equity/net-assets conditions;
- profitability;
- enforcement amount / revenue;
- active defendant claims / revenue;
- other validated ratios.

Each metric must retain period, source and formula.

## 4. Model stages

### Stage 1 — transparent rule/ratio layer

Use explainable financial ratios and trends. This provides immediate product value and a stable feature layer.

### Stage 2 — calibrated statistical model

Build a historical training sample:

`company state at date X -> financial/risk features at X -> bankruptcy/distress outcome in next 12/24 months`

Train and validate a transparent baseline such as logistic regression before more complex models.

The coefficients must come from real historical Russian-company data, not arbitrary manual weighting.

### Stage 3 — optional advanced models

Only after enough labelled data and proper validation. Advanced ML must be benchmarked against the transparent baseline and must not reduce explainability below an acceptable product/legal standard.

## 5. Output

The forecast output should contain:

- forecast horizon;
- model version;
- feature period;
- coverage/feature completeness;
- forecast class or calibrated probability only if validated;
- key drivers;
- limitations;
- explanation that the result is analytical, not an official bankruptcy fact.

Do not expose false precision before calibration.

## 6. Operational data / online cash registers

Public online-cash-register information is not assumed to expose the turnover of an arbitrary third-party company.

Future `Operational Distress Overlay` may use OFD/online-KKT data only when the company/client lawfully provides access to its own operational data or another valid legal basis exists.

Possible metrics:

- daily revenue;
- receipt count;
- average receipt;
- refund rate;
- revenue 30d/90d;
- MoM/YoY dynamics.

Absence of private OFD access does not reduce a company’s score and is not a negative signal. It simply means the operational overlay is unavailable/not applicable to that scenario.

Aggregated public geographic/industry KKT analytics may later be used as a benchmark, but not silently treated as the company’s own revenue.

## 7. Summary examples

Good factual wording:

“Revenue fell from 480m to 310m RUB from 2023 to 2025 (-35.4%). Equity fell from 74m to 18m RUB (-75.7%). Short-term liabilities reached 196m RUB.”

Then the model may state a calibrated forecast class/probability with model version and limits.

Bad wording:

“The company is likely bankrupt” without horizon, model version, data, coverage or distinction from an official procedure.

## 8. Validation requirements

Before production use of a forecast:

- define labelled outcome;
- prevent look-ahead leakage;
- time-based train/test split;
- calibration check;
- discrimination metrics;
- false positive/false negative review;
- segment review for small/large/new/special-sector companies;
- backtest by historical period;
- methodology versioning;
- human-readable feature explanation;
- documented limitations.

The forecast is not a substitute for official bankruptcy information, manual due diligence or legal advice.

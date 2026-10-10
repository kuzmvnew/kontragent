# Workspace Real Data UX Corrections 04B-01

Task: `WORKSPACE-REAL-DATA-UX-CORRECTIONS-04B-01`  
Canonical base: `0504498ef21505489ce9fd437c21e35785adad43`

## Baseline and affected components

The baseline was captured from the unmodified canonical base against the
existing disposable ООО «АЛАН» preview databases. It showed:

- mixed Unicode sidebar symbols with inconsistent geometry and alignment;
- an 18 px sidebar wordmark;
- the global-search icon overlapping the text area because the generic input
  selector overrode the component padding;
- raw contract/status vocabulary in customer-facing pages;
- repeated company identity and requisites in the hero and dossier body;
- a flat Company View rendering without stable subject-oriented navigation;
- action controls with different heights and denial text interrupting their
  baseline;
- unchecked domains presented without a sufficiently explicit coverage
  warning.

Affected presentation components are the Workspace shell, sidebar, global
search, authorized company card, Workspace operational list/detail templates,
Public Company Card, and the presentation-only label/grouping boundary.

No Public Projection field, Company View contract, route, permission, quota,
action, report payload, Data Core component, source integration, Risk/Summary
engine, migration, or retained source record was changed.

## Result

- All eight sidebar entries use one inline SVG icon system with a shared
  `24×24` viewBox, `1.8 px` stroke, and consistent rendered size.
- The established semantic `next. company` wordmark is 23 px without changing
  its proportions or markup.
- Global search preserves the existing GET behavior and now has a measured
  10 px clearance between the icon and text start at both tested widths.
- Legal status uses an explicit Russian map. Unknown technical status values
  fail closed as `Статус организации не определён`.
- Customer-facing enum and field labels pass through a presentation boundary;
  contract IDs and revisions remain in diagnostic data attributes and API
  contracts.
- Company cards follow company → status → key conclusion → actions, with core
  requisites rendered once in the `Реквизиты` section.
- The required ten-topic navigation is present on Public and authorized cards.
  Unchecked or missing coverage explicitly says that it does not prove absence
  of facts, events, debts, or restrictions.
- Save, Monitoring, Report, and Public Card controls retain their real actions
  while sharing a 44 px control height and stable baseline.

## Chromium evidence

| Surface | Before | After |
| --- | --- | --- |
| Public · 1440×1000 | [before-public-desktop.png](before-public-desktop.png) | [after-public-desktop.png](after-public-desktop.png) |
| Public · 390×844 | [before-public-mobile.png](before-public-mobile.png) | [after-public-mobile.png](after-public-mobile.png) |
| Workspace · 1440×1000 | [before-workspace-desktop.png](before-workspace-desktop.png) | [after-workspace-desktop.png](after-workspace-desktop.png) |
| Workspace · 390×844 | [before-workspace-mobile.png](before-workspace-mobile.png) | [after-workspace-mobile.png](after-workspace-mobile.png) |

The final real-data browser run asserted:

- exact 1440×1000 and 390×844 viewports;
- no document-level horizontal overflow on either surface;
- eight equal-geometry sidebar icons;
- wordmark computed size at least 22 px;
- long-query input, keyboard focus, visible focus ring, and 10 px icon/text
  clearance;
- aligned action heights (maximum difference 1 px);
- all required thematic links;
- no visible `COMPANY VIEW`, `company-view-v1`, `IDENTITY`, `ACTIVE`,
  `CURRENT`, or `NOT_ACTIVE` tokens.

## Regression evidence

- Workspace 03A–03D, Public Card, visual/browser, local-preview contracts:
  `341 passed, 9 skipped`.
- Workspace 03E full product E2E in fresh disposable operational/public
  databases with a read-only Public web role: `2 passed`.
- Real Alan Public/Login/Search/Save/Monitoring/Report E2E: `1 passed`.
- Presentation and contract-label focused suite: `39 passed`.
- `git diff --check`: passed.

The Real Alan E2E used a pre-test clone of the disposable operational preview.
After the run, the modified clone was removed and the pre-test clone was
restored. Restored counts were `reports=2`, `subscriptions=1`, `sessions=3`,
`events=0`, and `feed=0`. The retained source database and disposable Public
projection database were read-only throughout this task.

## Remaining limitations

- No new data source or business function was added. Courts, bankruptcy,
  licenses, and other uncovered domains remain explicitly marked as unchecked
  where the existing Company View has no coverage.
- Source descriptions and dates reflect the frozen ООО «АЛАН» preview release;
  this work does not refresh or re-ingest them.
- Navigation is in-page thematic navigation, not a new client-side router.

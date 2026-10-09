# Workspace Visual UX Polish 04A

Task: `MAC-OFFLINE-WORKSPACE-VISUAL-UX-POLISH-04A-CORE-SHELL-CARD-01`

This slice changes only the authenticated Workspace presentation layer. Functional product completion 03A–03E remains the accepted baseline. No route, input name, CSRF field, permission, entitlement, quota, tenant boundary, data model, source semantic, PublicProjection semantic, report snapshot, bulk state machine, monitoring event, invitation, or membership rule is changed.

## Before-state audit

The accepted functional UI used a dense horizontal header, a large multiline Workspace context block, six equal dashboard metrics, serif product headings up to 64 px, a gradient page background, shadowed content cards, a dark summary hero, two-column fact/source card walls, and raw entitlement keys as the primary settings labels.

## Implemented visual system

- Canonical ink, forest, sage, surface, border, muted, success, attention, and critical tokens are declared once in `workspace.css`.
- Product typography uses `PT Sans Caption`, `PT Sans`, Arial, and sans-serif fallbacks. Desktop H1 is 38 px, tablet H1 is 32 px, and mobile H1 is 28 px.
- The page background is white. Ordinary content has no decorative shadow or gradient.
- Radius values are restricted to 8, 12, and 16 px tokens.
- Buttons are at least 44 px high, inputs 46 px, and the Dashboard primary search 52 px.
- Reusable shell and operational primitives cover navigation, headers, search, KPI, panels, data rows, status badges, actions, facts, sources, analysis, usage, and empty states.

## Workspace shell

Desktop uses a persistent 224 px sidebar and 64 px top bar inside a 1440 px maximum product frame with 28 px outer margins. The navigation exposes only real routes and is grouped as Работа, Команда, and Аккаунт. One semantic navigation tree is reused as a compact four-column grid below 980 px, so the mobile layout does not retain a persistent sidebar and the active item has a single `aria-current="page"` source of truth.

The top bar contains the existing `GET /app/search?q=` action, compact Workspace/user/role context, Workspace switching, and logout. The Demo warning and `noindex` policy are unchanged. The canonical `next. company` wordmark is semantic text with accessible name `NEXT Company` and no image dependency.

## Dashboard

The Dashboard now uses four truthful metrics from the existing view model: saved companies, active monitoring, unread events, and saved-company usage/limit. Recent saved companies and monitoring events are scan-first data rows. Empty states remain actionable without nested decorative cards.

## Authorized Company Card

The header keeps the accepted company identity and action contracts while tightening hierarchy and quota metadata. A local anchor navigation is rendered only for sections on the page. NEXT Analysis is a light operational block with unchanged conclusion, status, date, factor, source, and limitation wording. Company View facts are dossier-style sections and rows. Source coverage is a dense table-like list. Limitations, recommendations, and monitoring remain explicit operational sections.

## Settings

Usage remains numeric and explicit in compact operational rows. Entitlement keys remain visible as secondary technical metadata, while customer-facing labels are primary: Массовая проверка, Мониторинг, Отчёты, Сохранённые компании, Пользователи, and Рабочее пространство.

## Public Card consistency audit

The Public site is intentionally not redesigned in 04A. The authenticated card continues to share the same company identity, status, source, freshness, limitation, and recommendation grammar while adopting the canonical Workspace token and typography system. Public routes, rendering, indexing behavior, and projection semantics are untouched; a full Public visual modernization remains a separate slice.

## Acceptance evidence

`tests/test_workspace_visual_polish.py` freezes tokens, typography, no-gradient/no-shadow rules, reusable primitives, live routes, and acceptance-relevant action/data attributes.

`tests/test_workspace_visual_polish_playwright.py` exercises real Chromium at 1440×1000 and 390×844 for Dashboard, authorized Company Card, and Settings. It asserts the shell, active navigation, search, four KPI model, product H1 scale, company actions, facts, sources, readable settings labels, visible keyboard focus, 44 px mobile controls, and zero page-level horizontal overflow. When `WORKSPACE_VISUAL_ARTIFACT_DIR` is set, it produces the six requested safe screenshots.

Committed QA artifacts:

- `docs/qa/workspace-visual-ux-polish-04a/01-dashboard-desktop.png`
- `docs/qa/workspace-visual-ux-polish-04a/02-dashboard-mobile.png`
- `docs/qa/workspace-visual-ux-polish-04a/03-company-desktop.png`
- `docs/qa/workspace-visual-ux-polish-04a/04-company-mobile.png`
- `docs/qa/workspace-visual-ux-polish-04a/05-settings-desktop.png`
- `docs/qa/workspace-visual-ux-polish-04a/06-settings-mobile.png`

## Release boundary

This work is implementation and local QA evidence only. It does not assert independent QA acceptance, merge, deployment, production mutation, source-live execution, mass ingestion, or production release.

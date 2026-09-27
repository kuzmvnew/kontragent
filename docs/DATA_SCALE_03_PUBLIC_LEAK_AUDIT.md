# DATA-SCALE-03-FINAL: public semantic leak audit

Audit baseline: `origin/main` at `32334077b8f6a0ab77219aeee1aaed75c3389ae9`.

This inventory was recorded before the semantic correction. It lists every
normal public path that could expose an internal representation directly.

| Surface | Location before correction | Leaking value |
| --- | --- | --- |
| Company HTML | `public_app/templates/company.html` risk badge | `projection.risk.state.value` (`FOUND`, `PARTIAL`, `STALE_DATA`, and other internal enums) |
| Company HTML | `public_app/templates/company.html` source heading | `source.code` (`REVEXP`, `PAYTAX`, `DEBTAM`, `TAXOFFENCE`) |
| Company HTML | `public_app/templates/company.html` source badge | `source.state.value` (internal state enums) |
| Company HTML | `public_app/templates/company.html` factor list | raw Risk v3 `title`/`fact`/`factor_code` and arbitrary `explanation` copied by the exporter |
| Company HTML | `public_app/templates/company.html` limitations | arbitrary strings copied from Risk/Summary v3, including serialized dictionaries and limitation codes |
| Company HTML | `public_app/templates/company.html` recommendations | arbitrary strings copied from Summary v3, including serialized recommendation dictionaries and recommendation codes |
| Company HTML | `public_app/templates/company.html` engine metadata | internal `model_version`, ruleset-adjacent product terminology, and release identifier |
| Company API | `GET /api/company/{inn}` in `public_app/main.py` | the full stored `PublicProjection`, including source codes, enum values, model/ruleset versions, and any unsafe legacy strings |
| Exporter | `scripts/export_public_release.py:_risk_projection` | fallback from `factor_code`, unknown `source_code`, arbitrary factor `explanation`, `limitation_code`, or complete arbitrary string |
| Exporter | `scripts/export_public_release.py:_summary_projection` | `str(value)` over `main_reasons`, `limitations`, and `recommendations`; dictionaries therefore became Python repr text |
| Exporter | `scripts/export_public_release.py:_source_block` | arbitrary `check["limitation"]` string and internal source code retained as the visible heading |

Internal persistence remains traceable and may retain codes and references.
The correction must ensure that none of those values are emitted by the
ordinary public HTML or public company API unless an approved deterministic
Russian-language template compiles them.

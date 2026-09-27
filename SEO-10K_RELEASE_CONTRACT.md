# SEO-10K Release Contract

Version: **1.0**

Date: **2026-09-27**

Status: **PREPARED / NOT RELEASED**

This is the canonical release contract for the NEXT Company SEO 10K program.
Implementation of the foundation does not authorize publication. Wave 500,
Wave 2000 and Wave 10000 remain **NOT RELEASED** until a later, explicit,
versioned release manifest passes every required gate. A `NOT_RUN` required
gate is release-blocking and can never be averaged into a pass.

## Source and revision boundary

One accepted `PublicProjection` revision is the only source for public HTML,
API and SEO output. An SEO projection must identify that same active release
revision and must never combine facts from different revisions. There is no
second identity store, semantic engine or SEO-only copy of company facts.

The legacy `publication.index_eligible` field is not proof of SEO eligibility.
Every URL is compiled independently by the versioned SEO eligibility compiler.
Unknown input, missing input, an unsupported schema, an unknown enum and a
compiler error all fail closed; none may default to `INDEX`.

## Eligibility decisions

- `INDEX`: every required gate passes for an active released legal entity.
- `NOINDEX_RECOVERABLE`: the card can remain visible, but a repairable content,
  identity, freshness, dispute, canonical or compiler gate failed.
- `NOT_PUBLISHED`: no public URL exists for an unreleased, unready, excluded or
  unsupported entity.
- `GONE`: an explicitly withdrawn entity returns the later release policy's
  gone response and is absent from sitemap/catalog membership.

`public_ready=true` alone never implies `INDEX`. A current supported schema,
valid checksum-verified 10-digit legal-entity INN, intact identity, semantic
evidence, freshness/dispute safety, forbidden-field scan, thin-content gate,
canonical integrity, active release membership, duplicate/legal/test exclusion
and crawlable internal-link membership are all required.

Only legal entities are in scope. A 12-digit individual-entrepreneur INN is not
eligible and must never be coerced to a legal entity or receive a numeric score.

## Semantic and thin-content gates

The required sequence is: **Факт → Аналитика NEXT → Что проверить перед
сделкой**. `INDEX` requires all of:

1. Four core identity facts, including name and INN.
2. Three additional entity-specific facts.
3. Two accepted public source references.
4. At least one dated source fact.
5. One visible Analytics block linked to a visible fact.
6. One visible Action block linked to a visible fact.
7. Visible source/result dates and explicit freshness wording.
8. No placeholder-only mandatory block.

Word count and unlinked generated prose are not eligibility signals. A page is
thin if it is an identity-only template, lacks sources or a dated fact, has an
unlinked Analytics/Action block, has a majority of unknown/unavailable semantic
blocks, or is effectively empty after identity removal. The compiler produces a
deterministic `non_identity_content_hash`; a service must flag clusters of 50 or
more template-identical pages for review.

## Search presentation

- H1: public short name; `ИНН {INN}` is separate.
- Title: `{Краткое наименование} — ИНН {INN}: сведения о компании | NEXT`.
- Fallback title: `{Полное наименование} — ИНН {INN} | NEXT`.
- Description and OpenGraph fields use only allowlisted neutral visible facts.
- Debt, offences, adverse/disputed/stale assertions, internal codes, raw enums,
  recommendation codes, bands, ratings and Numeric NEXT Index are forbidden in
  metadata.
- Unknown/unavailable/not-found/stale/disputed are distinct public states. They
  must never be rewritten as a positive conclusion.

SSR JSON-LD is an `@graph` with `WebPage`, `Organization` as `mainEntity`, and
`BreadcrumbList`. It contains only visibly rendered allowlisted fields.
`Review`, `AggregateRating`, `ratingValue`, `score`, `ClaimReview`, Numeric NEXT
Index and stale/disputed adverse facts are forbidden.

`PUBLIC_NEXT_INDEX_ENABLED` remains `False`. Numeric scores, 0–100 output,
ratings, bands and derived numeric reliability are forbidden recursively in
HTML, metadata, OG, JSON-LD, SEO projection, sitemap and catalog serialization.

## URL, robots and crawl contract

The only company canonical is
`https://nextcompany.pro/companies/{checksum-valid-10-digit-INN}`. It is
absolute, SSR and self-referencing. HTTP, `www`, legacy `/company/{INN}`, a
trailing slash and safe tracking-only parameters normalize with a permanent
308. Invalid INNs are real 404 responses. Company pages are never keyed by name
slug, KPP or OGRN, and internal links use canonical paths only.

Production `robots.txt` is exactly compatible with:

```text
User-agent: *
Allow: /
Disallow: /internal/
Disallow: /admin/
Disallow: /docs
Disallow: /openapi.json
Sitemap: https://nextcompany.pro/sitemap.xml
```

`/api/` is not blocked and every API response sends
`X-Robots-Tag: noindex, nofollow, nosnippet`. `/search` and catalog query/sort/
filter variants are `noindex,follow` and absent from sitemaps. Preview and
staging require access control beyond robots.

## Sitemap and catalog

`/sitemap.xml` is a sitemap index for `/sitemaps/static.xml.gz` and exactly 16
stable company shards, `/sitemaps/companies-00.xml.gz` through
`/sitemaps/companies-0f.xml.gz`. The shard is the first four bits of SHA-256(INN), so wave
changes never move an INN. Membership requires `INDEX`, active released cohort,
HTTP 200, self-canonical and `index,follow`. Redirect, noindex, error and
unreleased URLs are forbidden. `lastmod` is the visible-content update time;
technical republish cannot change it. `priority` and `changefreq` are forbidden.

The crawlable catalog is `/companies` plus `/companies/page/{N}`, 24 companies
per page. `/companies/page/1` is a 308 to the base. Pages 2+ self-canonical.
Empty/out-of-range pages are 404. Previous, next, first and nearby page links
are real SSR anchors. No region/industry catalog is authorized. Every indexable
company must be linked from this indexable catalog; otherwise eligibility fails.

## Freshness, hash and storage

A stale non-core module may remain visible when all other gates pass, but stale
adverse values are excluded from metadata, OG, JSON-LD and snippet-eligible
text. Stale/unknown/disputed core identity is `NOINDEX_RECOVERABLE` and is
removed from sitemap/catalog. Source unavailability cannot manufacture a
positive statement.

`search_visible_hash` covers deterministic search-visible output and excludes
release timestamps, job IDs, internal codes and debug fields. Technical no-op
republish keeps the hash and `lastmod`; a visible fact/content change changes
the hash. SEO state belongs only in the isolated public PostgreSQL release
snapshot. Operational PostgreSQL is not modified. Schema changes require a
versioned upgrade/downgrade, data preservation and mixed-version compatibility.

## Cohort and demand boundary

Typed cohort values 500, 2000 and 10000 are planning values, not release
authority. Only a later active release manifest can publish a URL. An unreleased
company is a 404, not a placeholder. The existing 40-card release is not
grandfathered into `INDEX`, and this implementation must not alter its
production state.

Live Wordstat and Google Ads historical metrics, final DemandScore, production
cache purge, Search Console/Yandex Webmaster observation and cohort activation
are outside this implementation. The future demand evidence schema must carry
query-set and provider-snapshot identity, exactly 12 monthly values, median,
average, entity ambiguity, completeness, algorithm version and wave assignment.
Unavailable credentials leave live evidence `NOT_RUN`; fixtures are never live
evidence and Keysso is not an approved provider.

## Release rule

Local unit, integration, browser and migration tests may prove only their own
gates. Production performance, 5× bot peak, live search metrics, webmaster
observation and deployment remain `NOT_RUN` until separately evidenced. This
contract authorizes no deployment, merge, sitemap submission or cohort change.

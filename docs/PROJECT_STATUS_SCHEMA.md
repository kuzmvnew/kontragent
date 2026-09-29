# Canonical project status

`project_status.yaml` is the only authoritative current project-status record.
`PROJECT_STATUS.md` is a deterministic generated view and must never be edited
manually. Historical status text is preserved under
`docs/archive/project-status/`.

## Schema version 1.0.0

The machine-readable contract is
`schemas/project_status.schema.json` (JSON Schema draft 2020-12). JSON Schema
validates shape and scalar types; `scripts/project_status.py` adds cross-field
and evidence rules which cannot be expressed clearly as structural schema.

The required top-level fields are:

| Field | Meaning |
|---|---|
| `schema_version` | Contract version; currently `1.0.0`. |
| `generated_at` | Status observation/update time, not file mtime. |
| `main_sha` | Canonical repository main SHA observed for the snapshot. |
| `code` | Facts present in Git/code only. |
| `qa` | QA acceptance for an explicit SHA. |
| `production` | Runtime/deployment facts backed by production evidence. |
| `user_visible` | Independent external-site/API/SSR observations. |
| `active_task` | Current governance task. |
| `next_gate` | Next required decision or handoff. |
| `incidents` | Active incidents, or an explicit unverified state. |
| `services` | Per-service observed state and provenance. |
| `data_state` | Production data/release counters mirrored from `production`. |

Every important assertion carries evidence with an observation timestamp,
evidence kind, reference, and `VERIFIED` or `NOT_VERIFIED` status. Unknown
runtime values stay as the literal `UNKNOWN`; an empty incident or withheld list
does not assert that no such items exist unless its containing state is verified.

## Independent state layers

The four layers deliberately do not imply one another:

```text
CODE --no implication--> QA --no implication--> PRODUCTION --no implication--> USER_VISIBLE
```

- A code capability may be `MERGED` while QA is `NOT_ACCEPTED`.
- QA may be `ACCEPTED` while production remains `NOT_VERIFIED`.
- A production capability may be `DEPLOYED` while its user-visible capability is
  `NOT_VISIBLE` or `UNKNOWN`.
- `DEPLOYED` requires an explicit runtime SHA plus verified runtime, release, or
  production-handoff evidence. Git or QA evidence alone is rejected.
- `VISIBLE` and positive site/API/SSR states require a separate external runtime
  observation and `external_verified_at`.
- Accepted QA for a SHA other than current `main_sha` must be marked `STALE`.

The schema uses distinct capability objects for each layer, so fields such as
`deployed` cannot be smuggled into a code assertion; unknown properties fail
schema validation.

## Canonical commands

After deliberately editing `project_status.yaml`, run the single canonical
operator command:

```bash
uv run python scripts/project_status.py update
```

It validates the YAML against schema `1.0.0`, runs semantic and live freshness
checks, regenerates `PROJECT_STATUS.md`, and verifies byte-for-byte parity.

To refresh only repository-derived facts (main SHA, latest first-parent merged
PR, migration heads, and checked-in release artifact digest) without inventing
runtime state:

```bash
git fetch origin
uv run python scripts/project_status.py collect --git-ref origin/main
```

The collector updates `generated_at` and repository provenance. It deliberately
preserves QA, production, user-visible, incident, service, and runtime data.
Those values require their own evidence.

When the public runtime is intentionally in scope, the same collector can also
refresh only facts exposed by the public health/readiness endpoints:

```bash
uv run python scripts/project_status.py collect \
  --git-ref origin/main \
  --runtime-base-url https://nextcompany.pro
```

This verifies the landing page, `/api/health`, and `/api/ready`, then updates
the observed release ID/count and public web/API service states. It does not
infer runtime SHA, DB revisions, deployment time, readiness counters, SSR, or
cohort identity from those endpoints; unavailable values and their provenance
remain explicit.

Read-only validation uses:

```bash
uv run python scripts/project_status.py check --freshness live
```

## Freshness and CI semantics

Live/operator validation compares the current UTC clock to `generated_at` and
fails when the age is greater than 24 hours. Filesystem mtime is never used.

CI uses:

```bash
uv run python scripts/project_status.py check --freshness repository
```

Repository mode compares `generated_at` with the commit timestamp under test.
This still rejects a commit that packages a status snapshot more than 24 hours
older than that commit, but checking out a historical commit later does not make
that immutable snapshot fail solely because wall-clock time passed. The
schema, semantic checks, and generated-Markdown parity are identical in both
modes.

`--freshness off` exists only for targeted tests and recovery diagnostics. It is
not the canonical operator or CI workflow.

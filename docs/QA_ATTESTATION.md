# SHA-bound QA attestation

`qa-acceptance` is a merge check, not a deployment authorization. It accepts a
QA decision only for the exact current pull-request number, base SHA, and head
SHA, and only when a trusted issuer posts a valid structured attestation.

## Decision flow

1. Codex implements a change on a pull-request branch.
2. QA checks out and tests the exact PR HEAD SHA against the exact current base
   SHA.
3. An allowed issuer posts one v1 QA attestation as a PR issue comment.
4. The trusted default-branch workflow reads current PR metadata and comments
   through the GitHub API. It does not check out or execute the PR tree.
5. The workflow publishes a `qa-acceptance` check run on the current PR HEAD.
6. The check is green only for `qa_verdict=PASS`, `merge_allowed=true`, and
   exact current PR, base SHA, and head SHA bindings. Branch protection may
   then permit merge.
7. Production remains blocked until a separate production-acceptance mechanism
   authorizes deployment. `qa-acceptance` never supplies that authorization.

The first PR that introduces this workflow is a bootstrap exception: GitHub
does not execute a newly added `pull_request_target` workflow from an unmerged
PR. Merge this bootstrap PR only through the repository's existing governance.
Once the files are on the default branch, activate the required check described
below for subsequent PRs.

## Contract

The authoritative v1 schema is
`.github/qa-attestation.schema.v1.json`. The evaluator applies the same strict
contract without adding a runtime package dependency. Unknown and duplicate
JSON fields are rejected.

The three decisions are intentionally independent:

- `qa_verdict`: whether the recorded QA execution passed.
- `merge_allowed`: whether this QA attestation permits merge.
- `deploy_allowed`: deployment information only; the merge check does not use
  it as production authorization.

A green `qa-acceptance` therefore requires `qa_verdict=PASS` and
`merge_allowed=true`. It may, and normally will, carry
`deploy_allowed=false`.

## Standard QA comment

Post this as a pull-request issue comment. Text outside the markers is for
humans; the single fenced JSON object inside the markers is authoritative.
Replace every example value with facts from the actual QA run.

````markdown
## QA attestation

QA passed for the exact pull-request base and head SHAs shown below. This is not
production approval. The JSON block is authoritative.

<!-- QA-ATTESTATION:BEGIN v1 -->
```json
{
  "schema_version": "1.0",
  "task_id": "TASK-ID-FROM-QA",
  "repository": "kuzmvnew/kontragent",
  "pr_number": 123,
  "base_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "head_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "qa_verdict": "PASS",
  "tested_at": "2026-09-29T08:30:00Z",
  "tests": [
    {
      "name": "focused QA acceptance",
      "command": "uv run python -m pytest tests/test_feature.py -q",
      "result": "PASS",
      "counts": {
        "passed": 12,
        "failed": 0,
        "skipped": 0,
        "total": 12
      }
    }
  ],
  "evidence": [
    {
      "kind": "RUN",
      "id": "github-actions:123456789",
      "url": "https://github.com/kuzmvnew/kontragent/actions/runs/123456789"
    }
  ],
  "merge_allowed": true,
  "deploy_allowed": false,
  "issuer": {
    "login": "kuzmvnew",
    "identity_source": "GITHUB_COMMENT_AUTHOR",
    "repository_role": "OWNER"
  },
  "reviewer": {
    "login": "kuzmvnew",
    "repository_role": "OWNER",
    "independence": "INDEPENDENT_REVIEWER_NOT_ESTABLISHED"
  }
}
```
<!-- QA-ATTESTATION:END v1 -->
````

Evidence belongs in GitHub or another durable system. The comment records IDs,
URLs, and optional SHA-256 digests; it does not embed large binary evidence.

When correcting an attestation for the same HEAD, edit or delete the old
current-HEAD comment before adding a replacement. More than one trusted
attestation for the current HEAD fails closed, even when the copies agree.
Attestations for older HEADs may remain as history.

## States and exact binding

The published check has four machine states:

| State | Meaning | Check conclusion |
| --- | --- | --- |
| `PASS` | One valid trusted attestation matches repository, PR, current base, and current HEAD; QA passed and merge is allowed. | success |
| `FAIL` | The current decision rejects merge, or trusted input is malformed, foreign, duplicate, or conflicting. | failure |
| `MISSING` | No trusted attestation exists for the PR. | failure |
| `STALE` | Trusted attestation history exists, but none matches both required current base and head SHA bindings. | failure |

On `synchronize`, HEAD A's successful check remains attached to A. The trusted
workflow evaluates HEAD B and creates a non-successful check on B until a valid
attestation for B exists. A different PR number, repository, base SHA, or head
SHA cannot reuse an acceptance.

Every attestation retains its tested `base_sha`, and the canonical repository
policy requires it to equal the current PR base SHA. Base drift always produces
`STALE` and requires a fresh QA attestation, even when the PR HEAD is unchanged.
A push to `main` re-evaluates every open PR, so an old green check cannot
silently survive base drift.

## Identity and reviewer independence

`.github/qa-attestation-policy.json` is authoritative only after it reaches the
default branch. `allowed_issuer_logins` lists accounts allowed to issue QA
attestations. The JSON `issuer.login` must equal the authenticated GitHub
comment author, and `issuer.repository_role` must equal GitHub's
`author_association` for that comment.

Repository/project owners are not independent reviewers. An attestation whose
reviewer is the issuer, has role `OWNER`, or is listed in
`repository_owner_logins` is rejected if it claims
`INDEPENDENT_REVIEWER_ESTABLISHED`. The honest same-owner value is
`INDEPENDENT_REVIEWER_NOT_ESTABLISHED`.

The default policy records this fact but does not block the owner-defined QA
flow. Set `require_independent_reviewer` to `true` in a separately reviewed
default-branch change if independent review later becomes mandatory.

## Trust boundary

The security-sensitive workflow uses `pull_request_target`, comment, default
branch push, and manual events. Its trust boundary is:

- the validator, policy, and workflow are checked out explicitly from the
  repository's default branch;
- the PR checkout, PR scripts, PR Actions, and PR-produced artifacts are never
  executed or trusted as approval;
- official actions are pinned to immutable commit SHAs;
- the check publisher has a separate, narrowly scoped GitHub App identity;
- the built-in workflow token has only read access to contents, PRs, and
  issues; it cannot publish `qa-acceptance`;
- the publisher App private key is an environment secret, and the
  `qa-attestation-publisher` environment must allow only the `main` branch;
- PR metadata and comment author identity come from the authenticated GitHub
  API, not from fields supplied by PR code;
- comments from accounts outside `allowed_issuer_logins` cannot approve or
  deny a PR;
- malformed trusted blocks and duplicate/current conflicts fail closed;
- created, edited, and deleted comments all trigger re-evaluation.

An issuer may reference a test run or its hashes as evidence, but a PR-produced
artifact alone is never treated as independent approval. The allowed issuer's
authenticated comment is the approval act.

## Required-check activation

Do not configure this as **any source** or as generic **GitHub Actions**. GitHub
allows any person or integration with repository write permission to set a
named check. The required check therefore needs a dedicated expected-source
App, distinct from the GitHub Actions App used by PR workflows.

Before merging the bootstrap PR, create the publisher trust boundary:

1. Register a dedicated GitHub App, recommended name
   `kontragent-qa-attestation`. Give it only **Metadata: read** and
   **Checks: read and write** repository permissions, subscribe it to no
   events, and install it only on `kuzmvnew/kontragent`.
2. Create the repository environment `qa-attestation-publisher`. Under
   **Deployment branches and tags**, choose **Selected branches and tags** and
   allow only `main`. Do not add `refs/pull/*/merge`.
3. In that environment, set variable `QA_ATTESTATION_APP_ID` to the App's
   numeric App ID and secret `QA_ATTESTATION_APP_PRIVATE_KEY` to one generated
   App private key. Do not store the private key as a repository-level secret.
4. After this workflow is on `main`, open a test PR and confirm that it creates
   a check run named exactly `qa-acceptance` whose source is the dedicated App.

Then open **Settings → Rules → Rulesets** and create or update the active
ruleset targeting the default branch `main`:

1. Enable **Require status checks to pass**.
2. Add the status check named exactly **`qa-acceptance`**.
3. Select **`kontragent-qa-attestation`** (the dedicated App) as the expected
   source. Never select **any source** or generic **GitHub Actions**.
4. Keep the ruleset active and do not add `production-acceptance` as an alias
   for this check.
5. Require branches to be up to date before merging so branch freshness and
   mandatory exact-base QA binding remain aligned.
6. Enable the equivalent of **Do not allow bypassing** for administrators and
   repository roles unless the repository has a separately documented
   emergency process.

If the repository later belongs to an organization with GitHub Enterprise,
also add a ruleset **Require workflows to pass before merging** rule pinned to
`kuzmvnew/kontragent`, ref `main`, and
`.github/workflows/qa-acceptance.yml`. The validator exits non-zero for
`FAIL`, `MISSING`, and `STALE`, so the pinned required workflow is an
additional non-spoofable gate. This organization-level feature is not required
for the dedicated-App source design.

Verify the active rule from a non-exempt test PR: no attestation must block;
an exact-HEAD accepted attestation must unblock; a new commit must block again.
Do not report branch protection as active until that behavior is observed.

## Production boundary

No application deployment is performed by this system. A deployment workflow
must require a distinct trusted input or check, recommended name
`production-acceptance`. It must not infer production approval from any of:

- `qa-acceptance=PASS`;
- `qa_verdict=PASS`;
- `merge_allowed=true`;
- `deploy_allowed=true` in this comment alone.

Production authorization needs its own issuer policy, trust boundary, and
auditable artifact before any production workflow consumes it.

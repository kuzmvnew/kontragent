# Published baseline hash reconciliation

Task: `PUBLIC-SYNC-PUBLISHED-BASELINE-RECONCILIATION-01`

This runbook repairs only stale HOME
`public_projection_publications.last_published_hash` values. It does not build,
upload, import, accept, promote, roll back, or otherwise mutate a public release.
The public database is reached only through the existing host-key-pinned SSH
transport and its explicitly read-only `read_public_release.py` command.

The authorized incident boundary is:

- exact active release:
  `public-v1-20260927T040214Z-32334077-8f44d69a`;
- exact parent count: `40`;
- exact retained count: `39`;
- withdrawn evidence row, excluded from parsing and updates: `0100000614`;
- retained control: `0100000639`.

The utility fails closed unless the trusted active release membership exactly
equals the operational published-parent membership, every parent row names the
expected release, all 39 retained projections pass the current strict
`PublicProjection` contract, and every retained row already has the supported
projection/hash version metadata. It never changes version metadata. A legacy
or otherwise ambiguous retained version is a blocker, not an invitation to
rewrite it.

## Before QA and merge

Development and QA must use only isolated test data. Do not source production
credentials, connect to production, invoke public sync, or start a worker,
scheduler, or ingestion command.

Run the focused regression suite:

```bash
python -m pytest tests/test_publication_baseline_reconciliation.py -q
```

The suite proves the 39-update plan, hash-only apply, second-apply no-op, Alan
preservation, public-store read-only behavior, and all required fail-closed
branches.

## Production dry run after QA and merge

Run on HOME as the existing public-sync operator only after the merged commit
has been deployed. Source the same private HOME environment used by the public
synchronizer so `DATABASE_URL`, `PUBLIC_SSH_TARGET`,
`PUBLIC_SSH_KNOWN_HOSTS_FILE`, and `PUBLIC_SSH_IDENTITY_FILE` are present. Do
not start or invoke the synchronizer itself.

Use a private evidence directory and capture the deterministic JSON plan:

```bash
set -o pipefail
umask 077
install -d -m 0700 /home/mikhail/nextcompany-operational/reconciliation-evidence
cd /home/mikhail/nextcompany-operational/current
python -m scripts.reconcile_publication_baseline \
  dry-run \
  --expected-release-id public-v1-20260927T040214Z-32334077-8f44d69a \
  --expected-parent-count 40 \
  --expected-retained-count 39 \
  --expected-withdrawn-inn 0100000614 \
  --expected-retained-control-inn 0100000639 \
  | tee /home/mikhail/nextcompany-operational/reconciliation-evidence/published-baseline-dry-run.json
```

Require `result=PLAN_READY`, `parent_count=40`, `retained_count=39`,
`alan_mutation=0`, `public_database_mutation=0`, and retain the emitted
`plan_sha256`. Record the exact observed `mismatch_count`. Stop if any
expectation fails or the mismatch count is not the incident-approved value.

## Explicit fail-closed apply

The apply transaction first takes the synchronizer's PostgreSQL advisory lock,
then locks the full published parent inventory with `FOR UPDATE OF p`. Each
retained mismatch is updated with a compare-and-swap
predicate over its old hash, release, published state, and both version fields.
Only `last_published_hash` is assigned. Before commit the command performs a
fresh trusted-release read and requires a zero-mismatch post-plan; any change
to Alan or any retained non-hash field rolls the transaction back.

Replace `39` below only if the reviewed dry-run evidence and incident approval
name a different exact mismatch count:

```bash
cd /home/mikhail/nextcompany-operational/current
python -m scripts.reconcile_publication_baseline \
  apply \
  --expected-release-id public-v1-20260927T040214Z-32334077-8f44d69a \
  --expected-parent-count 40 \
  --expected-retained-count 39 \
  --expected-withdrawn-inn 0100000614 \
  --expected-retained-control-inn 0100000639 \
  --expected-mismatch-count 39 \
  --confirm-apply public-v1-20260927T040214Z-32334077-8f44d69a \
  | tee /home/mikhail/nextcompany-operational/reconciliation-evidence/published-baseline-apply.json
```

Require `result=APPLIED`, `update_count=39`,
`post_apply_mismatch_count=0`, `alan_mutation=0`,
`release_id_mutation=0`, `is_published_mutation=0`, and
`public_database_mutation=0`.

## Mandatory no-op rerun

Run the same controlled apply path with an exact expected mismatch count of
zero. This exercises the locks and all guards again without writing:

```bash
cd /home/mikhail/nextcompany-operational/current
python -m scripts.reconcile_publication_baseline \
  apply \
  --expected-release-id public-v1-20260927T040214Z-32334077-8f44d69a \
  --expected-parent-count 40 \
  --expected-retained-count 39 \
  --expected-withdrawn-inn 0100000614 \
  --expected-retained-control-inn 0100000639 \
  --expected-mismatch-count 0 \
  --confirm-apply public-v1-20260927T040214Z-32334077-8f44d69a \
  | tee /home/mikhail/nextcompany-operational/reconciliation-evidence/published-baseline-second-apply.json
```

Require `result=NO_OP`, `mismatch_count=0`, `update_count=0`, and all mutation
counters zero.

## Drift forensics

The repository history shows that the active release predates the later
transition-aware projection/hash version metadata and substantial strict
contract evolution. That establishes possible mechanisms by which a hash could
be interpreted under different semantics, but it does not prove how the 39
operational values were actually written. Without production audit evidence
linking the historical writer and exact stored values, record:

`ROOT CAUSE OF DRIFT: NOT VERIFIED.`

Do not infer or publish a more specific cause from the mismatch alone.

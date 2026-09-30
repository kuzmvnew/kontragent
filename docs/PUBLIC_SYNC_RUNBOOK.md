# NEXT Company automatic public sync

`nextcompany-public-sync.service` runs on HOME as the `mikhail` user through
the persistent user systemd manager (`loginctl ... Linger=yes`). It is the only
component allowed to read operational PostgreSQL and initiate outbound SSH to
the public VPS. The VPS never connects back to HOME.

## Safety boundary

- The accepted cohort is the checked-in, checksummed canonical manifest.
- A company can become dirty only from its latest successful, `public_ready`
  `CompanyEnrichmentRun` whose Risk v3 and Summary v3 are still current and
  linked.
- The semantic hash excludes release ID and publication time, but includes all
  visible identity, fact, freshness, Risk, Summary, limitation, result-date and
  index-eligibility fields.
- The checked-in cohort is the eligibility universe. Target release membership
  is the current legitimate `public_ready` subset; it is not required to have
  the same size as the parent release.
- Public HTTPS supplies only the active `release_id` and `record_count` identity.
  Retained parent rows are read as full `PublicProjection` payloads from the
  exact active public PostgreSQL release through the existing host-key-pinned
  SSH/importer boundary, with an explicitly read-only transaction. The ordinary
  `/api/company/{inn}` response remains a browser-safe public view and is not
  used as the retained-parent projection source.
- Every parent/target member is classified deterministically as `UNCHANGED`,
  `UPDATED`, `ADDED`, or `WITHDRAWN`. A withdrawn company is omitted instead of
  being copied from stale last-good content.
- Projection and semantic-hash versions are stored with the durable baseline.
  An explicit version upgrade is published as `UPDATED`; current-version hash
  drift for a retained company still fails closed.
- Build or schema failure never invokes the VPS importer. The VPS stages and
  accepts the candidate while the exact parent remains active, promotes only
  after acceptance, and persists HOME last-good state only after promoted HTTPS
  verification. A failed post-promotion check invokes exact-parent rollback.

## HOME installation

Before applying the operational migration, run the canonical operational
backup service and require both SHA256 verification and restore-test `PASS`.

Install the private environment and unit as `mikhail`:

```bash
install -d -m 0700 /home/mikhail/nextcompany-operational/public-sync
install -m 0600 deploy/env/public-sync.env.example \
  /home/mikhail/nextcompany-operational/runtime/public-sync.env
# Set PUBLIC_SSH_TARGET, PUBLIC_SSH_IDENTITY_FILE and
# PUBLIC_SSH_KNOWN_HOSTS_FILE to the already-authorized, host-key-pinned VPS
# transport.
install -m 0644 deploy/systemd/nextcompany-public-sync.service \
  /home/mikhail/.config/systemd/user/nextcompany-public-sync.service
systemctl --user daemon-reload
systemctl --user enable --now nextcompany-public-sync.service
```

The HOME SSH identity must be readable only by `mikhail`; the VPS host key must
already be pinned in `/home/mikhail/.ssh/known_hosts`. Never use
`StrictHostKeyChecking=no` or copy operational database credentials to the VPS.
The authorized VPS command boundary must permit the synchronizer's invocation
of `scripts/read_public_release.py` as `nextcompany-importer`. That reader accepts
only a safe exact release ID and sorted legal-entity INNs, verifies that the
release is still active and internally complete, and returns only the requested
stored projections over SSH. It does not create an HTTP route or write to the
public database.

## Acceptance

```bash
systemctl --user show nextcompany-public-sync.service \
  -p ActiveState -p UnitFileState -p MainPID -p NRestarts
curl --fail --silent --show-error https://nextcompany.pro/api/ready
```

Inspect durable state without modifying it:

```sql
SELECT status, count(*)
FROM public_publication_requests
GROUP BY status
ORDER BY status;

SELECT count(*) AS dirty_accepted_projections
FROM public_publication_requests
WHERE status IN ('PENDING','RETRY_SCHEDULED');
```

The first service cycle bootstraps last-good hashes and membership from the
exact live parent release. If membership and all current-ready projections are identical it records
`NO_PUBLIC_CHANGE` and performs no upload or import.

For the one-time controlled repair of a proven stale operational hash baseline,
use [PUBLICATION_BASELINE_RECONCILIATION.md](PUBLICATION_BASELINE_RECONCILIATION.md).
That procedure preserves the ordinary fail-closed parent hash validation; it is
not part of the automatic synchronizer and must not be substituted for a normal
publication transition.

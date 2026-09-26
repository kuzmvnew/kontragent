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
- Every candidate contains the full accepted cohort. A non-ready company is
  carried only from the exact live last-good projection and its recorded hash
  must still match.
- Build or schema failure never invokes the VPS importer. Import is atomic. A
  failed post-import HTTPS check invokes the canonical exact-parent rollback.

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

The first service cycle bootstraps last-good hashes from the exact live cohort.
If all current ready projections are identical it records
`NO_PUBLIC_CHANGE` and performs no upload or import.

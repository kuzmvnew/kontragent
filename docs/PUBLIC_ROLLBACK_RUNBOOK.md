# NEXT Company public rollback runbook

## Data release rollback

Read the active release and its exact previous release:

```sql
SELECT r.release_id, r.previous_release_id
FROM public_publication_state s
JOIN public_releases r ON r.release_id = s.active_release_id
WHERE s.singleton = TRUE;
```

Switch only to that exact ID:

```bash
set -a; source /etc/nextcompany/importer.env; set +a
/opt/nextcompany/current/.venv/bin/python \
  /opt/nextcompany/current/scripts/rollback_public_release.py \
  '<EXACT_PREVIOUS_RELEASE_ID>'
```

The transaction changes the active pointer and statuses; it does not delete either release. Verify `/api/ready`, three card URLs and the active release ID.

## Application rollback

Choose an existing exact SHA under `/opt/nextcompany/releases/`, then run:

```bash
sudo /opt/nextcompany/current/deploy/scripts/rollback_public_app.sh '<EXACT_RELEASE_SHA>'
```

This changes the application symlink atomically and checks loopback health. It does not alter public data.

## Full database restore

Use only when pointer rollback cannot recover service. Verify the `.sha256` file, preserve a new pre-restore backup, and run:

```bash
set -a; source /etc/nextcompany/importer.env; set +a
sudo /opt/nextcompany/current/deploy/scripts/restore_public_backup.sh \
  --backup /var/backups/nextcompany/nextcompany_public_<timestamp>.dump \
  --confirm RESTORE_PUBLIC_DB
```

After any rollback, rerun production acceptance. Keep failed releases and evidence until the incident is closed.

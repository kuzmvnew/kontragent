# NEXT Company public launch runbook

This runbook deploys `public_app.main:app`. The internal `main.py` is never exposed. The public server has no route, credentials, network dependency, or database link to the home operational worker.

## 1. Minimum host and network

- Ubuntu 24.04, 2 vCPU, 4 GB RAM, 40–60 GB SSD.
- Public IPv4, SSH access, outbound package/TLS access, and provider-level backup capability.
- PostgreSQL listens on loopback only. Uvicorn listens on `127.0.0.1:8000`; only Nginx listens on ports 80/443.
- DNS names: `nextcompany.pro` and `www.nextcompany.pro`.

Shared hosting without SSH, systemd, Nginx and a private PostgreSQL instance is unsupported. The exact result in that case is `PUBLIC_VPS_REQUIRED`.

## 2. Host bootstrap

Run as root on the public VPS:

```bash
apt-get update
apt-get install -y postgresql nginx certbot python3-certbot-nginx curl rsync postgresql-client util-linux
groupadd --system nextcompany
useradd --system --user-group --groups nextcompany --home /nonexistent --shell /usr/sbin/nologin nextcompany-web
useradd --system --user-group --groups nextcompany --home /nonexistent --shell /usr/sbin/nologin nextcompany-importer
install -d -o root -g nextcompany -m 0750 /opt/nextcompany
install -d -o root -g nextcompany -m 0750 /opt/nextcompany/shared
install -d -o nextcompany-importer -g nextcompany -m 0750 /var/lib/nextcompany/incoming
install -d -o postgres -g postgres -m 0700 /var/backups/nextcompany
install -d -m 0755 /var/www/letsencrypt
```

Install `uv` from its verified upstream package and make it available to root. The
deploy script sets `UV_PYTHON_INSTALL_DIR` to
`/opt/nextcompany/shared/python`, with the UV cache, configuration home, and
temporary UV home also under `/opt/nextcompany/shared/`. It never uses root's
default home for managed Python. Application releases and the shared managed
Python remain root-owned and non-writable by both runtime identities.

## 3. Isolated database and roles

Generate two independent strong passwords. Do not reuse operational credentials.

```sql
CREATE ROLE nextcompany_importer LOGIN PASSWORD '<IMPORT_PASSWORD>';
CREATE ROLE nextcompany_web LOGIN PASSWORD '<WEB_PASSWORD>';
CREATE DATABASE nextcompany_public OWNER nextcompany_importer;
```

Run the public migration as `nextcompany_importer`, then enforce web read-only privileges:

```sql
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO nextcompany_web;
GRANT SELECT ON public_releases, public_company_projections, public_publication_state TO nextcompany_web;
ALTER DEFAULT PRIVILEGES FOR ROLE nextcompany_importer IN SCHEMA public GRANT SELECT ON TABLES TO nextcompany_web;
```

In `postgresql.conf`, keep `listen_addresses = 'localhost'`. Do not add a rule that permits the home network to connect to this database.

## 4. Secrets and service installation

Copy `deploy/env/public.env.example` to `/etc/nextcompany/public.env`; it contains only the read-only web DSN. Set owner `root:nextcompany-web` and mode `0640`.

Copy `deploy/env/importer.env.example` to `/etc/nextcompany/importer.env`; it contains only the write DSN. Set owner `root:nextcompany-importer` and mode `0640`. The web user is not a member of the importer user's private group and cannot read this credential. No secret belongs in Git.

Both DSNs must use the explicit SQLAlchemy psycopg 3 dialect prefix
`postgresql+psycopg://`. The deploy script validates both files before changing
the active release. Backup and restore scripts convert that prefix internally
when invoking the libpq command-line tools; no second credential or DSN is
required.

Copy the systemd files to `/etc/systemd/system/`, and the Nginx file to `/etc/nginx/sites-available/nextcompany.pro`. Enable the site and services:

```bash
ln -s /etc/nginx/sites-available/nextcompany.pro /etc/nginx/sites-enabled/nextcompany.pro
systemctl daemon-reload
systemctl enable nextcompany-public.service nextcompany-backup.timer
```

`nextcompany-backup.service` runs as `postgres:postgres` with
`SupplementaryGroups=nextcompany`. That canonical supplementary group is
required to traverse the root-owned application release and execute the backup
helper; no manual drop-in is required. Keep the environment files at `0640`
with their documented private groups. systemd reads `EnvironmentFile=` before
starting the restricted service, so the importer secret does not need broader
file permissions. The backup directory stays `postgres:postgres` at `0700`.

For first certificate issuance, serve `/.well-known/acme-challenge/` on HTTP, point temporary validation DNS to the VPS if required by the provider, then run:

```bash
certbot certonly --webroot -w /var/www/letsencrypt -d nextcompany.pro -d www.nextcompany.pro
nginx -t && systemctl reload nginx
```

## 5. Atomic application deploy

Upload a clean Git checkout at the approved SHA to a staging directory, then run:

```bash
sudo deploy/scripts/deploy_public.sh /path/to/staged/checkout
```

The script validates that an existing `/opt/nextcompany/current` is an absolute,
direct symlink to a concrete directory under `/opt/nextcompany/releases/`. It
takes a pre-deploy backup when an existing release is present, syncs
dependencies, runs only `public_alembic.ini`, changes the symlink atomically,
restarts the application, then waits up to 45 seconds for an exact HTTP 200 from
the loopback `/api/health` endpoint. The one-second polling loop tolerates
initial connection refusals, resets, and other transient startup responses, but
stops immediately if systemd reports the unit failed and never waits beyond the
bounded startup window. Only after health succeeds does it validate and reload
Nginx. A terminal service failure or expired health window atomically restores
the validated exact prior release. If that prior directory has become invalid
or disappeared, the failed release is left inactive and deployment exits with
an explicit error; the script never creates `current -> current` or a link
outside the releases directory.

After every deploy, verify the runtime interpreter and both runtime identities:

```bash
runtime_python="$(readlink -f /opt/nextcompany/current/.venv/bin/python)"
printf '%s\n' "$runtime_python"
case "$runtime_python" in /root|/root/*|/home/root|/home/root/*) exit 1 ;; esac
sudo -u nextcompany-web /opt/nextcompany/current/.venv/bin/python -c 'import sys; print(sys.executable)'
sudo -u nextcompany-importer /bin/bash -c '
  set -a; source /etc/nextcompany/importer.env; set +a
  exec /opt/nextcompany/current/.venv/bin/alembic -c /opt/nextcompany/current/public_alembic.ini current
'
systemctl show nextcompany-backup.service -p User -p Group -p SupplementaryGroups
systemctl start nextcompany-backup.service
```

The resolved Python path must never be below `/root` or `/home/root`; a managed
interpreter should resolve below `/opt/nextcompany/shared/python`. The backup
unit must report `User=postgres`, `Group=postgres`, and
`SupplementaryGroups=nextcompany`, and must create a dump plus its SHA256 file
without a server-only drop-in.

## 6. Outbound-only 40-card publication

On the home worker, from the exact merged SHA:

```bash
set -a; source .env; set +a
export PUBLIC_SSH_TARGET='<ssh-alias-or-user@public-ip>'
scripts/publish_public_release.sh
```

The command reads `nextcompany_operational`, verifies the immutable checksum for
`docs/releases/public-v1-cohort-40.json`, exports only those 40 INNs, uploads the
versioned bundle over SSH/SFTP, invokes the public importer, and checks three
cards. The public host never contacts the operational database. A cohort change
requires a new canonical manifest and a new release ID; superseded cohort
evidence remains under `docs/releases/`.

## 7. Pre-cutover acceptance

Before DNS cutover, keep the old public DNS in place and test the new IP while sending the real hostname:

```bash
curl --resolve nextcompany.pro:443:<PUBLIC_IP> https://nextcompany.pro/api/ready
curl --resolve nextcompany.pro:443:<PUBLIC_IP> https://nextcompany.pro/companies/0274101890
```

Run the full acceptance from a host that resolves the test mapping:

```bash
python scripts/accept_public_launch.py \
  --bundle /path/to/public-v1-... \
  --worker-offline-proof
```

For the independence proof, block public-server egress to the home/private network at the VPS firewall (or stop the home worker), then rerun the acceptance. The active cards must remain available. Record the firewall rule or stopped-worker evidence with the acceptance output.

Acceptance must show HTTPS, apex/www redirect, landing, full-INN search, three cards, API, Risk/Summary, source/result dates, robots, sitemap, canonical, JSON-LD, 404, active release ID, and exact bundle correspondence.

## 8. DNS cutover

Only after pre-cutover acceptance passes:

1. Set apex `A` to the public IPv4.
2. Set `www` to the apex using the provider-supported `CNAME`/redirect arrangement.
3. Wait for authoritative answers, not browser cache.
4. Rerun `scripts/accept_public_launch.py` against real `https://nextcompany.pro`.
5. Confirm the worker-offline test again after cutover.

Do not copy the operational database or RAW ZIP/XML/PDF files to the public host.

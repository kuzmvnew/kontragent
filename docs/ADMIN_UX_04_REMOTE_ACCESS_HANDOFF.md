# ADMIN-UX-04 — remote access evidence and infrastructure handoff

## Current security boundary

The application supports two explicit modes:

- `local` keeps the existing loopback-only boundary. Authentication is optional only in this mode; setting all three credential variables enables it locally.
- `remote` fails closed unless an owner username, a versioned scrypt password hash, a session secret, explicit allowed hosts and trusted proxy addresses are present. It accepts only connections from the trusted proxy boundary and requires that boundary to set `X-Forwarded-Proto: https`.

The service continues to bind only to `127.0.0.1:8081`. It must never be changed to `0.0.0.0`, exposed through a HOME router port-forward, or served over plain HTTP.

Generate the password hash interactively (the plaintext is not echoed or stored):

```console
cd /home/mikhail/nextcompany-runtime/current
.venv/bin/python -m scripts.generate_admin_password_hash
```

Create `/home/mikhail/nextcompany-operational/runtime/admin.env` mode `0600` with:

```dotenv
ADMIN_ACCESS_MODE=remote
ADMIN_USERNAME=<owner login>
ADMIN_PASSWORD_HASH=<scrypt-v1 output>
ADMIN_SESSION_SECRET=<at least 32 random characters>
ADMIN_SESSION_TTL_SECONDS=28800
ADMIN_ALLOWED_HOSTS=admin.nextcompany.pro
ADMIN_TRUSTED_PROXY_IPS=127.0.0.1,::1
```

## INFRA HANDOFF → 27

Remote network publication was not part of the accepted application boundary and remains blocked on infrastructure. The infrastructure task must provide all of the following before remote access can be marked verified:

1. `admin.nextcompany.pro` (or another explicitly approved admin hostname) with DNS pointing only to the trusted HTTPS edge.
2. A valid TLS certificate and HTTPS-only reverse proxy; HTTP must redirect before credentials are accepted.
3. An authenticated private tunnel from the HTTPS edge to HOME. The application target remains `127.0.0.1:8081`; no HOME router forwarding and no public listen socket.
4. Proxy configuration that overwrites (does not append/pass through) `Host: admin.nextcompany.pro` and `X-Forwarded-Proto: https`. The backend authorizes the socket peer from `ADMIN_TRUSTED_PROXY_IPS` and does not trust `X-Forwarded-For` for access control.
5. Firewall rules allowing the tunnel only. Port 8081 must not be Internet-reachable.
6. The root-owned or owner-readable `admin.env` above, mode `0600`, with secrets supplied out-of-band. No plaintext password is stored.
7. External verification of TLS, unauthenticated redirect, valid and invalid login, session expiry, logout invalidation, CSRF/foreign-Origin rejection, cookie flags and `/admin/health` monitoring.

The health probe is `GET /admin/health`. It intentionally contains only service/database status and is the sole unauthenticated application probe besides login and static login assets.

## Confirmed source-link model

Catalog metadata comes from the 45 `DataSet` rows and their `DataSource` owners in operational PostgreSQL. Runtime connection/status comes separately from Worker Foundation. The missing canonical workbook is not reconstructed.

The UI keeps publisher pages separate from access coordinates. Representative confirmed contracts are:

| Type | Dataset | Official page | Data access |
|---|---|---|---|
| Bulk/file discovery | `fns_revenue_expenses` | `https://www.nalog.gov.ru/` | DataSet open-data release page |
| API | `cbr_finorg` | `https://www.cbr.ru/` | DataSet `FO_ZoomWS/FinOrg.asmx` endpoint |
| Browser registry/search | `moscow_general_court_cases` | `https://mos-gorsud.ru/` | DataSet search page |
| Access-required integration | `eis_procurements` | `https://zakupki.gov.ru/` | DataSet EIS integration service coordinate |
| Dynamic artifact discovery | `mintrans_ted_registry` | `https://mintrans.gov.ru/` | Stable official search query from `MintransOfficialProvider`; temporary `/file/{id}` artifacts are not exposed as catalog metadata |

`safe_url` accepts only HTTP(S), rejects embedded credentials, removes fragments and removes credential/signature query keys before rendering.

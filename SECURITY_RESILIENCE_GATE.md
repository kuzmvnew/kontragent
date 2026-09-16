# SECURITY & RESILIENCE GATE — Kontragent

Status: APPROVED MANDATORY PRE-SEO GATE
Date: 2026-09-17

Mass publication/indexing of the first 10,000 company pages is forbidden until this gate passes.

## 1. Edge / DDoS / anti-bot

- CDN/reverse proxy/WAF in front of public application;
- DDoS protection;
- origin not openly bypassable without justified need;
- rate limits for search, cards, auth, reports, exports and API-like endpoints;
- bot/scraping controls that do not block legitimate SEO crawlers;
- stronger limits for anomalous datacenter/proxy/sequential scraping traffic;
- separate quotas for export/bulk/API;
- abuse logging and rapid blocking process;
- cloud cost/budget alerts.

## 2. Web/API security

- OWASP-oriented review;
- input validation;
- SQL injection/XSS/CSRF protections where applicable;
- secure headers/cookies;
- safe redirects/path/header handling;
- dependency/security scanning;
- secret scanning;
- secrets never stored in Git or logs.

## 3. Accounts and critical access

- MFA/passkeys for GitHub, hosting/cloud, DNS/domain registrar and admin services;
- least privilege;
- administrative interfaces separated/protected;
- audit of critical admin actions;
- credential stuffing/brute-force protection when user accounts are live.

## 4. PostgreSQL and data

- PostgreSQL not directly exposed to public internet;
- firewall/security groups;
- least-privileged DB users;
- query/connection limits;
- automatic backups;
- PITR or equivalent recovery for production when justified;
- backup stored separately from production;
- backup is accepted only after successful restore test.

## 5. Safe source updates

Preferred pattern:

`download -> checksum/validate -> staging -> import -> validate -> atomic publish`

A failed new dataset must not destroy the last known-good published state.

Keep ingestion history, dataset versions and rollback/recovery information.

## 6. Deploy safety

- production separated from staging;
- tests before production deployment;
- migrations have rollback/recovery plan;
- production release linked to commit/version;
- ability to restore previous working release;
- repository/cloud access protected;
- home PC is not the only copy of code or production state.

## 7. Monitoring and alerts

Monitor at least:

- website/API uptime;
- 4xx/5xx/error spikes;
- CPU/RAM/disk;
- PostgreSQL connections and slow queries;
- background/ingestion jobs;
- suspicious traffic/scraping;
- certificate expiry;
- disk exhaustion;
- cloud spending anomalies;
- failed dataset updates.

Alerts must reach the operator, not exist only in logs.

## 8. Disaster recovery

Document short procedures for:

- server loss;
- DB corruption;
- broken migration/deploy;
- credential/key compromise;
- DNS/domain problem;
- home worker loss;
- source update corruption.

Use a resilient backup strategy; critical data must not exist as a single copy on one device/server.

## 9. Home PC rule

Home Windows PC may be a dev/batch/worker node. Its power/internet failure must not take the public production service offline.

## 10. Gate acceptance

Gate passes only when:

- public edge protection/rate limiting are configured;
- origin/DB exposure reviewed;
- MFA and secret hygiene confirmed;
- tested restore exists;
- rollback/recovery tested;
- monitoring + real alerts work;
- basic security and load/abuse tests pass;
- disaster recovery instructions exist.

Failure of a critical item blocks 10k SEO publication but does not prevent internal development.

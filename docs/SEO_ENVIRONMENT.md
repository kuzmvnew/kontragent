# SEO MVP environment variables

Do not commit real credentials. Put them in local/production environment secrets only.

Required before live Wordstat collection:

- `YANDEX_WORDSTAT_API_KEY` — API key for a Yandex Cloud service account with Search API / Wordstat access.
- `YANDEX_WORDSTAT_FOLDER_ID` — Yandex Cloud folder ID if required by the selected Search API setup.

Required before public production indexing:

- `PUBLIC_BASE_URL` — final HTTPS production origin, for example `https://nextprofile.ru`.
- `INDEXNOW_KEY` — 8–128 character key exposed on the same host according to IndexNow rules.
- `YANDEX_METRIKA_COUNTER_ID` — one Metrica counter for the main product and company pages.

Google after production-domain setup:

- Google Search Console Domain property verified by DNS.
- GA4 Measurement ID for one web data stream.
- Optional API/service-account credentials only if automated exports are later required.

Never put API keys, OAuth tokens, cookies, DNS credentials, Google service account JSON, or Metrica access tokens into GitHub.

# atsb-ingest

Ingests **ATSB** (Australian Transport Safety Bureau, `atsb.gov.au`) aviation
investigations into a standalone SQLite.

ATSB sits behind Akamai, which resets the HTTP/2 stream for any client whose
TLS/H2 fingerprint isn't a real browser, and the investigations listing is
JS-hydrated. So egress is a **headed Chromium** driven by Playwright with a
persistent profile (to keep the Akamai clearance cookie warm). See
[`src/scrape.js`](src/scrape.js) for the full rationale.

## Requirements

- Node ≥ 20
- Playwright + Chromium (only needed to actually scrape; the parser/tests don't):
  ```bash
  npm i playwright && npx playwright install chromium
  ```
- A display, or `xvfb-run` on a headless host (headed Chromium is required —
  headless gets fingerprint-blocked).

## Usage

```bash
npm install

# build (headed browser; on a server wrap with xvfb-run)
xvfb-run -a node src/cli.js build --db ./atsb-accidents.sqlite
node src/cli.js build --max-pages 5            # just the newest 5 listing pages
```

Useful env vars: `ATSB_PROFILE_DIR` (persist the browser profile / clearance
cookie across runs), `ATSB_PROXY` (`socks5://…` to rotate egress IP),
`ATSB_HEADLESS=1` (override — usually blocked). Re-runs skip the detail fetch
for investigations that already have a probable cause.

## Layout

| File | Role |
|------|------|
| `src/parse.js` | Pure cheerio parsers: `parseListingPage`, `parseDetail` |
| `src/scrape.js` | Playwright headed-browser egress (`createScraper`) |
| `src/db.js` | Standalone SQLite schema + upsert (one row per investigation) |
| `src/cli.js` | discover → fetch → parse → build orchestration |
| `test/parse.test.js` | Offline parser tests against committed HTML fixtures |

## Tests

```bash
npm test       # jest, fully offline — no browser needed
```

## Data

ATSB reports are public-record safety investigations published under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) by the Commonwealth of
Australia; attribute the ATSB if you redistribute. This package is the ingest
software only (Apache-2.0).

# ntsb-ingest

Builds a standalone SQLite database of **US NTSB** civil aviation accident
reports from NTSB's public bulk dump (`avall.zip`).

Unlike the web-scraping sources in this repo, NTSB publishes its entire
accident database as a downloadable Microsoft Access dump. This package
downloads it, exports the relevant tables with `mdbtools`, joins them, and
writes a single `accidents` table with the factual / analysis / probable-cause
narratives plus metadata (aircraft, location, injuries, weather, flight phase).

## Requirements

- Node ≥ 20
- `mdbtools` (provides `mdb-export`) and `unzip` on `PATH`
  - macOS: `brew install mdbtools`
  - Debian/Ubuntu: `apt-get install mdbtools unzip`

## Usage

```bash
npm install

# logic check — no download, exercises the join/mapping on sample data
npm run selftest

# download the latest NTSB dump and build the SQLite (default ./ntsb-accidents.sqlite)
npm run build               # or: node src/cli.js build ./out.sqlite
```

The build downloads ~hundreds of MB from `data.ntsb.gov`, so it takes a while
and needs temp space (override the temp root with `NTSB_TMPDIR`). NTSB refreshes
the dump roughly monthly — re-run to regenerate.

## Layout

| File | Role |
|------|------|
| `src/parse.js` | Pure join/mapping helpers (`joinNtsbTables`, `buildWeatherSummary`, `buildFactorsJson`) |
| `src/cli.js` | Download → unzip → `mdb-export` → join → write SQLite |
| `test/parse.test.js` | Offline unit tests for the parser |

## Tests

```bash
npm test       # jest, fully offline
```

## Data

NTSB accident data is US-government public-record. This package is the builder
software only (Apache-2.0); the data is subject to NTSB's terms.

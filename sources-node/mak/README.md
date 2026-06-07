# mak-ingest

Ingests **MAK** (Interstate Aviation Committee / Межгосударственный авиационный
комитет, `mak-iac.org`) accident reports into a standalone SQLite.

MAK publishes a per-year index of investigations; each links to an HTML detail
page and, for completed investigations, a final-report PDF (Russian). This
package walks the yearly index, parses each detail page into structured columns,
and for final reports downloads the PDF and extracts the *История полёта*
(history of flight) and *Заключение* (conclusion / probable cause) sections.

## Usage

```bash
npm install

# build the full archive (current year → 2004, descending so PDF-bearing
# reports surface first); default ./mak-accidents.sqlite
npm run build

node src/cli.js build --year 2018 --db ./mak.sqlite     # a single year
node src/cli.js build --from 2014 --to 2026              # a range
```

MAK's Bitrix front-end rate-limits aggressively, so requests are paced at 2.5s
with bounded retries — a full build is intentionally slow. Re-runs are
idempotent: detail pages are re-upserted and a PDF is only re-downloaded when no
real probable-cause has been extracted yet.

## Layout

| File | Role |
|------|------|
| `src/parse.js` | Pure parsers: `parseYearListing`, `parseDetail` (HTML) and `extractNarrative` (PDF) |
| `src/scrape.js` | Paced, retrying `fetch` tuned for MAK's CDN + `listYear` |
| `src/db.js` | Standalone SQLite schema + upsert (one row per slug) |
| `src/cli.js` | discover → fetch → parse → build orchestration |
| `test/parse.test.js` | Offline parser tests against committed HTML fixtures |

## Tests

```bash
npm test       # jest, fully offline (HTML fixtures committed under test/fixtures/)
```

## Data

MAK reports are public-record investigation documents published by the
Interstate Aviation Committee and subject to its terms. This package is the
ingest software only (Apache-2.0).

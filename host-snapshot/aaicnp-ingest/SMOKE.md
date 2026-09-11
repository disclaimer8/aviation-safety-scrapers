# Nepal AAIC (CAAN) Phase-1 smoke (bounded discover + fetch + parse + build)

Source key: `aaicnp` (Aircraft Accident Investigation Commission, Nepal — under CAAN).
Country: NP. Tables: `aaicnp_reports` / `aaicnp_accidents`.

## Where the PDFs live
Nepal final reports are SCATTERED — there is no single clean HTML index:
- most on the gov CDN: `https://giwmscdnone.gov.np/media/pdf_upload/...`
- some on `https://caanepal.gov.np/storage/app/media/...`
- a chronological accident/incident *record list* (reg + date, no report links)
  lives on the CAAN Safety Management Division accidents/incidents page.

Discovery harvests report-PDF hrefs from CAAN listing surfaces (SMD pages,
all-news + news-detail posts, all-notice + notice-detail pages) FOLLOWING each
href to whatever host, plus a seed list of known gov/CDN final reports.

## Procedure
```bash
.venv/bin/python -m aaicnp_ingest.cli discover --db smoke.db
.venv/bin/python -m aaicnp_ingest.cli fetch    --db smoke.db --pdf-dir smoke-pdfs
.venv/bin/python -m aaicnp_ingest.cli parse    --db smoke.db
.venv/bin/python -m aaicnp_ingest.cli build    --db smoke.db

# Inspect
.venv/bin/python -c "
import sqlite3
c=sqlite3.connect('smoke.db')
print('reports:', c.execute('SELECT COUNT(*) FROM aaicnp_reports').fetchone()[0])
print('by status:', dict(c.execute('SELECT status,COUNT(*) FROM aaicnp_reports GROUP BY status').fetchall()))
print('accidents:', c.execute('SELECT COUNT(*) FROM aaicnp_accidents').fetchone()[0])
for r in c.execute('SELECT case_id,registration,event_date,LENGTH(narrative_text),site_slug FROM aaicnp_accidents LIMIT 5'):
    print(' ', r)
"
rm -f smoke.db smoke.db-wal smoke.db-shm && rm -rf smoke-pdfs
```

## Expected
- discover: inserts report rows keyed by a provisional filename-slug id.
- fetch: downloads PDFs (Referer required for the CDN); failures stay 'new'.
- parse: re-derives INTRINSIC case_id from the PDF text
  (report reference `Aircraft Accident Investigation Report N/YYYY` -> AAIC-NP-N-YYYY,
  else registration+date), scanned-aware (text <~500 chars -> tier 'scanned').
- build: English text-layer reports projected into aaicnp_accidents (country=NP).

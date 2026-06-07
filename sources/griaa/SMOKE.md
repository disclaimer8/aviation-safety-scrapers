# GRIAA Phase-1 smoke (bounded discover + fetch + parse + build)

GRIAA = Grupo de Investigación de Accidentes (DIACC), Aerocivil, Colombia.
Source: https://www.aerocivil.gov.co/investigacion/Accidentes/

## Procedure

```bash
# 1. Bounded discover — one year via the GET year filter (no captcha needed)
.venv/bin/python -c "
import httpx, griaa_ingest.griaa as g, griaa_ingest.db as db
conn = db.connect('smoke.db'); db.init_schema(conn)
client = httpx.Client(headers=g.HEADERS, follow_redirects=True, timeout=60)
url = g.year_url(2025)
html = client.get(url).text
rows = g.parse_listing(html, url)
for r in rows[:10]:
    print(r['case_id'], r['date_of_occurrence'], r['event_class'], r['registration'])
print('rows in 2025:', len(rows))
conn.close(); client.close()
"

# 2-4. Real pipeline stages on smoke.db (full backfill uses `all`)
.venv/bin/python -m griaa_ingest.cli discover --db smoke.db
.venv/bin/python -m griaa_ingest.cli fetch    --db smoke.db --pdf-dir smoke-pdfs
.venv/bin/python -m griaa_ingest.cli parse    --db smoke.db
.venv/bin/python -m griaa_ingest.cli build    --db smoke.db

# 5. Inspect results
.venv/bin/python -c "
import sqlite3
c = sqlite3.connect('smoke.db')
print('reports:', c.execute('SELECT COUNT(*) FROM griaa_reports').fetchone()[0])
print('accidents:', c.execute('SELECT COUNT(*) FROM griaa_accidents').fetchone()[0])
print('tiers:', dict(c.execute('SELECT source_tier, COUNT(*) FROM griaa_reports GROUP BY source_tier').fetchall()))
c.close()
"

# 6. Clean up
rm -f smoke.db smoke.db-wal smoke.db-shm && rm -rf smoke-pdfs
```

## Expected result

- discover: walks per-year listings (1998-present) via ?inicio=Y&fin=Y; inserts rows into griaa_reports
- fetch: downloads the preferred PDF (Final > Prelim) with UA + Referer
- parse: pdftotext extracts text, strips the ADVERTENCIA legal preamble;
  tier=pdf (usable text) / scanned (image-only, <500 chars) / none
- build: tier='pdf' rows with narrative >= 80 chars projected into
  griaa_accidents (country=CO); scanned/none rows skipped

# JTSB Phase-1 smoke (bounded discover + fetch + parse + build)

JTSB uses plain httpx — no browser/Xvfb required. Official English PDFs are
fetched directly from the JTSB airrep.html listing.

## Procedure

```bash
# 1. Bounded discover — walk airrep.html listing
python -c "
import httpx, jtsb_ingest.jtsb as j, jtsb_ingest.db as db, jtsb_ingest.pipeline as p
conn = db.connect('smoke.db')
db.init_schema(conn)
client = httpx.Client(headers=j.HEADERS, follow_redirects=True)
n = p.discover(conn, client)
print('discovered:', n)
conn.close()
client.close()
"

# 2-4. Real pipeline stages on smoke.db
.venv/bin/python -m jtsb_ingest.cli fetch  --db smoke.db --pdf-dir smoke-pdfs
.venv/bin/python -m jtsb_ingest.cli parse  --db smoke.db
.venv/bin/python -m jtsb_ingest.cli build  --db smoke.db

# 5. Inspect results
python -c "
import sqlite3
c = sqlite3.connect('smoke.db')
print('reports:', c.execute('SELECT COUNT(*) FROM jtsb_reports').fetchone()[0])
print('accidents:', c.execute('SELECT COUNT(*) FROM jtsb_accidents').fetchone()[0])
c.close()
"

# 6. Clean up
rm -f smoke.db smoke.db-wal smoke.db-shm && rm -rf smoke-pdfs
```

## Expected result

- discover: walks jtsb.mlit.go.jp/en/aircraft/airrep.html, inserts rows into jtsb_reports
- fetch: downloads available English PDFs; rows without PDF advance with pdf_path=NULL
- parse: pdftotext extracts narratives; tier=pdf/short/none
- build: rows with narrative >= 80 chars projected into jtsb_accidents (country=JP)

# ANSV Phase-1 smoke (bounded discover + fetch + parse + build)

## Procedure

```bash
# 1. Bounded discover — walk WordPress paginated listing
python -c "
import httpx, ansv_ingest.ansv as a, ansv_ingest.db as db, ansv_ingest.pipeline as p
conn = db.connect('smoke.db')
db.init_schema(conn)
client = httpx.Client(headers=a.HEADERS, follow_redirects=True)
n = p.discover(conn, client)
print('discovered:', n)
conn.close()
client.close()
"

# 2-4. Real pipeline stages on smoke.db
.venv/bin/python -m ansv_ingest.cli fetch  --db smoke.db --pdf-dir smoke-pdfs
.venv/bin/python -m ansv_ingest.cli parse  --db smoke.db
.venv/bin/python -m ansv_ingest.cli build  --db smoke.db

# 5. Inspect results
python -c "
import sqlite3
c = sqlite3.connect('smoke.db')
print('reports:', c.execute('SELECT COUNT(*) FROM ansv_reports').fetchone()[0])
print('accidents:', c.execute('SELECT COUNT(*) FROM ansv_accidents').fetchone()[0])
c.close()
"

# 6. Clean up
rm -f smoke.db smoke.db-wal smoke.db-shm && rm -rf smoke-pdfs
```

## Expected result

- discover: walks ansv.it WordPress paginated listing, inserts rows into ansv_reports
- fetch: downloads available PDFs; rows without PDF advance with pdf_path=NULL
- parse: pdftotext extracts narratives; tier=pdf/short/none
- build: rows with narrative >= 80 chars projected into ansv_accidents (country=IT)

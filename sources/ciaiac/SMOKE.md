# CIAIAC Phase-1 smoke (bounded discover + fetch + parse + build)

## Procedure

```bash
# 1. Bounded discover — walk a few year pages
python -c "
import httpx, ciaiac_ingest.ciaiac as c, ciaiac_ingest.db as db, ciaiac_ingest.pipeline as p
conn = db.connect('smoke.db')
db.init_schema(conn)
client = httpx.Client(headers=c.HEADERS, follow_redirects=True)
n = p.discover(conn, client)
print('discovered:', n)
conn.close()
client.close()
"

# 2-4. Real pipeline stages on smoke.db
.venv/bin/python -m ciaiac_ingest.cli fetch  --db smoke.db --pdf-dir smoke-pdfs
.venv/bin/python -m ciaiac_ingest.cli parse  --db smoke.db
.venv/bin/python -m ciaiac_ingest.cli build  --db smoke.db

# 5. Inspect results
python -c "
import sqlite3
c = sqlite3.connect('smoke.db')
print('reports:', c.execute('SELECT COUNT(*) FROM ciaiac_reports').fetchone()[0])
print('accidents:', c.execute('SELECT COUNT(*) FROM ciaiac_accidents').fetchone()[0])
c.close()
"

# 6. Clean up
rm -f smoke.db smoke.db-wal smoke.db-shm && rm -rf smoke-pdfs
```

## Expected result

- discover: walks transportes.gob.es year pages (2008-present), inserts rows into ciaiac_reports
- fetch: downloads available EN/ES PDFs; rows without PDF advance with pdf_path=NULL
- parse: pdftotext extracts narratives; tier=pdf/short/none
- build: rows with narrative >= 80 chars projected into ciaiac_accidents (country=ES)

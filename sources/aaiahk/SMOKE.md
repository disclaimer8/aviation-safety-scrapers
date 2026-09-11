# HK AAIA Phase-1 smoke (bounded discover + fetch + parse + build)

Source: https://www.tlb.gov.hk/aaia/eng/investigation_reports/index.html
One server-rendered register page (a <table>); case_id from the anchor TEXT
(IVR-/ITR-/PLR-YYYY-NN), NOT the href filename. English text-layer PDFs.

## Procedure

```bash
. .venv/bin/activate

# 1. Live discover (whole register page; ~44 rows, ~33 IVR final reports)
python -c "
import aaiahk_ingest.aaiahk as a, aaiahk_ingest.db as db, aaiahk_ingest.pipeline as p
conn=db.connect('smoke.db'); db.init_schema(conn)
client=a.make_client()
print('discovered:', p.discover(conn, client))
conn.close(); client.close()
"

# 2. Bound to a few IVR rows, then run the real pipeline stages
python -c "
import aaiahk_ingest.db as db
conn=db.connect('smoke.db')
keep=[r[0] for r in conn.execute(\"SELECT case_id FROM aaiahk_reports WHERE case_id LIKE 'IVR-%' ORDER BY case_id LIMIT 4\")]
conn.execute('UPDATE aaiahk_reports SET status=\"skipped\" WHERE case_id NOT IN (%s)' % ','.join('?'*len(keep)), keep)
conn.commit(); conn.close()
"
python -m aaiahk_ingest.cli fetch --db smoke.db --pdf-dir smoke-pdfs
python -m aaiahk_ingest.cli parse --db smoke.db
python -m aaiahk_ingest.cli build --db smoke.db

# 3. Inspect
python -c "
import sqlite3; c=sqlite3.connect('smoke.db')
print('accidents:', c.execute('SELECT COUNT(*) FROM aaiahk_accidents').fetchone()[0])
"

# 4. Clean up
rm -f smoke.db smoke.db-wal smoke.db-shm && rm -rf smoke-pdfs
```

## Expected result (verified 2026-06-07)

- discover: 44 rows (33 IVR final + 11 ITR/PLR interim/preliminary)
- fetch: PDFs download via browser UA + Referer (no 403 / JS challenge)
- parse: pdftotext extracts 34K-83K chars per IVR report; tier='pdf'
- build: rows >= 80 chars projected into aaiahk_accidents (country='HK')

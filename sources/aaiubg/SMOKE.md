# Bulgaria AAIU (aaiubg) Phase-1 smoke (bounded discover + fetch + parse + build)

Source: https://www.mtc.government.bg/en/category/193  (ENGLISH PDFs)
Source key: aaiubg  (NOT aaiu=Ireland, NOT aaiube=Belgium)

## Procedure

```bash
# 1. Discover — walk index + per-year document pages
.venv/bin/python -m aaiubg_ingest.cli discover --db smoke.db

# 2-4. Pipeline stages
.venv/bin/python -m aaiubg_ingest.cli fetch --db smoke.db --pdf-dir smoke-pdfs
.venv/bin/python -m aaiubg_ingest.cli parse --db smoke.db
.venv/bin/python -m aaiubg_ingest.cli build --db smoke.db

# 5. Inspect
python -c "
import sqlite3
c = sqlite3.connect('smoke.db')
print('reports:', c.execute('SELECT COUNT(*) FROM aaiubg_reports').fetchone()[0])
print('accidents:', c.execute('SELECT COUNT(*) FROM aaiubg_accidents').fetchone()[0])
c.close()
"

# 6. Clean up
rm -f smoke.db smoke.db-wal smoke.db-shm && rm -rf smoke-pdfs
```

## Expected result

- discover: ~28 English reports across per-year pages inserted into aaiubg_reports
- fetch: downloads EN PDFs (Referer header required)
- parse: pdftotext extracts narratives; tier=pdf/short/none
- build: rows with narrative >= 80 chars projected into aaiubg_accidents (country=BG)
- case_id is INTRINSIC: <REG>_<YYYY-MM-DD>, filename fallback when unregistered

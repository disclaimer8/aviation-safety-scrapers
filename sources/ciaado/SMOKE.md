# CIAA (Dominican Republic) Phase-1 smoke

Source: https://ciaa.gob.do  (Joomla + Phoca Download; per-year categories)
case_id: 'CIAA-NNN-YYYY' (from 'caso NNN-YY[YY]' in the report title)
country_iso: DO   |   language: Spanish (EN translation is downstream)

## Procedure

```bash
# 1. Full discover — walk 3 top categories → per-year subcategories
.venv/bin/python -m ciaado_ingest.cli discover --db smoke.db

# 2-4. fetch + parse + build (fetch downloads ALL 'new' rows; throttled 1.8s)
.venv/bin/python -m ciaado_ingest.cli fetch  --db smoke.db --pdf-dir smoke-pdfs
.venv/bin/python -m ciaado_ingest.cli parse  --db smoke.db
.venv/bin/python -m ciaado_ingest.cli build  --db smoke.db

# 5. Inspect
.venv/bin/python -c "
import sqlite3
c=sqlite3.connect('smoke.db')
print('reports:',  c.execute('SELECT COUNT(*) FROM ciaado_reports').fetchone()[0])
print('accidents:',c.execute('SELECT COUNT(*) FROM ciaado_accidents').fetchone()[0])
c.close()"

# 6. Clean up
rm -f smoke.db smoke.db-wal smoke.db-shm && rm -rf smoke-pdfs
```

## Observed (live, 2026-06-07)

- discover: 118 reports (119 Phoca download links − 1 dedup collision)
  across 2008-2026; Final reports walked first so they win over a colliding
  Preliminary/Provisional document for the same case number.
- fetch: gated Phoca '?download=ID:slug' URLs followed verbatim with a Referer
  (the subcategory page); stream real text-layer PDFs.
- parse: pdftotext yields full Spanish narratives (sample: 16181 / 9144 / 9137
  chars); tier='pdf' when >= 600, 'short' below, 'scanned' when no text layer.
- build: rows with narrative >= 80 chars projected to ciaado_accidents
  (country='DO'); ciaado_accidents column set is IDENTICAL to ciaiac_accidents
  (P2 prod-sync requirement).

# NSIB Phase-1 smoke (discover -> fetch -> parse -> build)

NSIB (Nigeria Safety Investigation Bureau) — https://nsib.gov.ng/air-reports/

```bash
# 1. Discover (walks all listing pages; INTRINSIC case_id; ~32 rows inserted)
.venv/bin/python -m nsib_ingest.cli discover --db smoke.db

# 2. Fetch the report PDFs (direct wp-content/uploads hrefs; no ninja-forms gate)
.venv/bin/python -m nsib_ingest.cli fetch  --db smoke.db --pdf-dir smoke-pdfs

# 3. Parse (pdftotext; OCR fallback only fires on a thin/scanned text layer).
#    Use --no-ocr off the mini-PC (no ocrmypdf/tesseract).
.venv/bin/python -m nsib_ingest.cli parse  --db smoke.db --no-ocr

# 4. Build (project into nsib_accidents, country=NG; thin <80c skipped)
.venv/bin/python -m nsib_ingest.cli build  --db smoke.db

# 5. Inspect
.venv/bin/python -c "
import sqlite3; c=sqlite3.connect('smoke.db')
print('reports:',   c.execute('SELECT COUNT(*) FROM nsib_reports').fetchone()[0])
print('accidents:', c.execute('SELECT COUNT(*) FROM nsib_accidents').fetchone()[0])
"

# 6. Clean up
rm -f smoke.db smoke.db-wal smoke.db-shm && rm -rf smoke-pdfs
```

## Expected result (2026-06)

- discover: ~32 rows (14 preliminary reports + 18 interim statements).  The ~86
  "Final Report" rows carry NO downloadable PDF and are not ingested.  The blank
  NSIB Form-001 template row (status 'Report') is filtered out.
- fetch: downloads the report PDFs (direct hrefs; doubled-filename artifact
  trimmed at the first .pdf).
- parse: pdftotext extracts narratives (prelims ~10-21K chars, interims ~2-6K
  chars); all are text PDFs so OCR almost never fires.  tier = pdf/ocr/short/none.
- build: rows with narrative >= 80 chars projected into nsib_accidents
  (country=NG).  ~32 accidents.

## case_id (INTRINSIC)

Priority: structured PDF-path ref (e.g. `AAL/2024/12/11/INTR/01`) ->
`NSIB-<TYPE>-<REG>-<DATE>` (reg from listing cell or filename, type = PRE/INT/FIN)
-> reg-only / date-only fallback.  Never the post URL/slug.

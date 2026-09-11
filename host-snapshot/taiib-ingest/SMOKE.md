# TAIIB (Latvia) Phase-1 smoke (discover + fetch + parse + build)

## Procedure

```bash
# 1. Discover — parse the single accordion listing page
.venv/bin/python -m taiib_ingest.cli discover --db smoke.db        # ~31

# 2-4. Fetch / parse / build on smoke.db
.venv/bin/python -m taiib_ingest.cli fetch --db smoke.db --pdf-dir smoke-pdfs
.venv/bin/python -m taiib_ingest.cli parse --db smoke.db
.venv/bin/python -m taiib_ingest.cli build --db smoke.db

# 5. Inspect
python -c "
import sqlite3
c = sqlite3.connect('smoke.db')
print('reports:', c.execute('SELECT COUNT(*) FROM taiib_reports').fetchone()[0])
print('accidents:', c.execute('SELECT COUNT(*) FROM taiib_accidents').fetchone()[0])
c.close()
"

# 6. Clean up
rm -f smoke.db smoke.db-wal smoke.db-shm && rm -rf smoke-pdfs
```

## Expected result

- discover: parses https://www.taiib.gov.lv/lv/aviacijas-nobeiguma-zinojumi-0
  (⚠️ trailing "-0" is mandatory; without it 302-redirects) → ~31 rows into
  taiib_reports. case_id is INTRINSIC (reference / registration+date / media-id).
- fetch: downloads the Drupal media-download PDFs (/lv/media/<id>/download).
- parse: pdftotext extracts narratives; tier=pdf/short/scanned/none.
  Image-only scans yield 0 chars → tier='none'/'scanned'.
- build: text-layer reports (>=80 chars, not 'scanned') → taiib_accidents
  (country='LV'). Observed 2026-06-08: 31 discovered, 28 built, 3 skipped
  (image-only scans). avg narrative ~41k chars.

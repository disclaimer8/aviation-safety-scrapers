# AACSV (El Salvador AAC) Phase-1 smoke

Single WPDM datatable at https://www.aac.gob.sv/informes-de-accidentes/ (page_id=1006).

```bash
# 1. discover (live) — parse the WPDM datatable
.venv/bin/python -m aacsv_ingest.cli discover --db smoke.db        # -> 45

# 2. (optional) bound fetch to a few representative slugs, then run stages
.venv/bin/python -m aacsv_ingest.cli fetch --db smoke.db --pdf-dir smoke-pdfs
.venv/bin/python -m aacsv_ingest.cli parse --db smoke.db
.venv/bin/python -m aacsv_ingest.cli build --db smoke.db

# 3. inspect
.venv/bin/python -c "import sqlite3;c=sqlite3.connect('smoke.db');\
print('reports',c.execute('SELECT COUNT(*) FROM aacsv_reports').fetchone()[0]);\
print('accidents',c.execute('SELECT COUNT(*) FROM aacsv_accidents').fetchone()[0])"

# 4. clean up
rm -f smoke.db smoke.db-wal smoke.db-shm && rm -rf smoke-pdfs
```

## Notes
- Download URLs are /download/<slug>/?wpdmdl=<id> (the &refresh=<token> param is dropped).
- SPANISH text-layer PDFs; narrative_text kept in Spanish (P3 ES->EN downstream).
- Scanned gate: pdftotext < MIN_NARRATIVE (500) -> tier 'short'/'none'; informe-final-8
  is a scan (broken XRef, ~0 chars) -> tier 'none' -> skipped at build.
- build() dedups final-over-preliminary per occurrence (registration+year key);
  aacsv_accidents PK = intrinsic case_id AAC-AIG-<NNN>-<REG>-<YYYY>; country 'SV'.

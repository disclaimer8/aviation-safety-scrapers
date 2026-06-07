# CIAA Peru (ciaape) Phase-1 smoke

Source: CIAA / MTC on gob.pe (multi-hop, server-rendered).
- Hub collection (paginated): /institucion/mtc/colecciones/383-...-ciaa?sheet=N
- Report page: /institucion/mtc/informes-publicaciones/{id}-informe-...-ciaa-...
- PDF: https://cdn.www.gob.pe/uploads/document/file/{id}/{name}.pdf (Spanish, text-layer)

## Procedure

```bash
# 1. Live discover — walks every collection sheet until empty
.venv/bin/python -c "
import httpx, ciaape_ingest.ciaape as c, ciaape_ingest.db as db, ciaape_ingest.pipeline as p
conn = db.connect('smoke.db'); db.init_schema(conn)
client = httpx.Client(headers=c.HEADERS, follow_redirects=True, timeout=c.TIMEOUT)
print('discovered:', p.discover(conn, client))
client.close(); conn.close()
"

# 2-4. Real pipeline stages
.venv/bin/python -m ciaape_ingest.cli fetch  --db smoke.db --pdf-dir smoke-pdfs
.venv/bin/python -m ciaape_ingest.cli parse  --db smoke.db
.venv/bin/python -m ciaape_ingest.cli build  --db smoke.db

# 5. Inspect
.venv/bin/python -c "
import sqlite3; c=sqlite3.connect('smoke.db')
print('reports:', c.execute('SELECT COUNT(*) FROM ciaape_reports').fetchone()[0])
print('accidents:', c.execute('SELECT COUNT(*) FROM ciaape_accidents').fetchone()[0])
"

# 6. Clean up
rm -f smoke.db smoke.db-wal smoke.db-shm && rm -rf smoke-pdfs
```

## Verified (live, 2026-06-07)

- discover: 199 distinct case_ids across collection sheets (Accident 118, Incident 72, Serious incident 9). Raw listing ~268 results; dedup of provisional+final sharing a case_id and the privacy-policy footer filter account for the difference.
- fetch: report-page hop → cdn.www.gob.pe PDF download (Referer required).
- parse: pdftotext; scanned gate < 500 chars → 'scanned' (skipped). Peru narratives are legitimately thin (~2.4K typical) — 'short'/'pdf' tiers both build.
- build: country='PE'; ciaape_accidents column set IDENTICAL to ciaiac_accidents (P2 prod sync depends on this).
- 4-report bounded fetch+build sample: all built, 26K–123K narrative chars, case_ids CIAA-ACCID-006-2025 / CIAA-INCID-011-2018 / CIAA-SINCID-001-2025 / CIAA-SINCID-005-2025; regs OB-2019P, OB-1882-P, OB-1870, HP-1844CMP.
- pytest: 52 passed.

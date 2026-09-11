# Mongolia AAIB (aaibmn) Phase-1 smoke

Source: https://aaib.gov.mn  — EN report category /en/c/report (paginated).
Report PDFs are MIXED: recent (2024+) have a text layer; older (≤2021) are
SCANNED images (pdftotext → 0 chars) → pipeline falls back to the listing title.

## Geo-route note
The host is reachable from ANY vantage in testing (no working geo-block
observed). The scraper still supports the DE SOCKS route via --proxy.

```bash
# DE SOCKS tunnel (run-cycle does this automatically on the mini-PC)
ssh -fND 127.0.0.1:1080 hetzner

# Pipeline via the DE proxy
.venv/bin/python -m aaibmn_ingest.cli discover --db smoke.db --proxy socks5://127.0.0.1:1080
.venv/bin/python -m aaibmn_ingest.cli fetch    --db smoke.db --pdf-dir smoke-pdfs --proxy socks5://127.0.0.1:1080
.venv/bin/python -m aaibmn_ingest.cli parse    --db smoke.db
.venv/bin/python -m aaibmn_ingest.cli build    --db smoke.db

# Inspect
.venv/bin/python -c "import sqlite3;c=sqlite3.connect('smoke.db');\
print('reports', c.execute('SELECT COUNT(*) FROM aaibmn_reports').fetchone()[0]);\
print('accidents', c.execute('SELECT COUNT(*) FROM aaibmn_accidents').fetchone()[0])"

rm -f smoke.db smoke.db-wal smoke.db-shm && rm -rf smoke-pdfs
pkill -f "ssh -fND 127.0.0.1:1080 hetzner" || true
```

## Expected
- discover: ~27 reports (29 PDF rows minus intrinsic case_id dedup of MN/EN/RU dupes)
- fetch: PDFs to pdfs/ via the proxy
- parse: tier=pdf (recent) / scanned (older) / none (no PDF)
- build: country=MN; scanned rows use the title as narrative

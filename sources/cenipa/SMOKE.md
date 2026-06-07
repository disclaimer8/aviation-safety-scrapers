# CENIPA Phase-1 smoke (bounded discover + fetch + parse + build)

## Requirements

```bash
# Install Playwright Chromium (one-time, on the mini-PC or local dev):
.venv/bin/playwright install chromium

# Browser steps (discover, fetch) need a real display on the mini-PC:
# They use headed Chromium to pass the Cloudflare JS challenge.
# Wrap in xvfb-run on a headless server:
#   xvfb-run -a python -m cenipa_ingest.cli discover --db smoke.db
# Or pass --headless (may fail Cloudflare challenge — for dev use only).
```

## Procedure

```bash
# 1. Bounded discover — walk CENIPA listing with Playwright/Xvfb
xvfb-run -a .venv/bin/python -m cenipa_ingest.cli discover \
    --db smoke.db --max-pages 1

# 2-4. Real pipeline stages on smoke.db
xvfb-run -a .venv/bin/python -m cenipa_ingest.cli fetch \
    --db smoke.db --pdf-dir smoke-pdfs
.venv/bin/python -m cenipa_ingest.cli parse --db smoke.db
.venv/bin/python -m cenipa_ingest.cli build --db smoke.db

# 5. Inspect results
python -c "
import sqlite3
c = sqlite3.connect('smoke.db')
print('reports:',   c.execute('SELECT COUNT(*) FROM cenipa_reports').fetchone()[0])
print('accidents:', c.execute('SELECT COUNT(*) FROM cenipa_accidents').fetchone()[0])
c.close()
"

# 6. Clean up
rm -f smoke.db smoke.db-wal smoke.db-shm && rm -rf smoke-pdfs
```

## Expected result

- discover: walks sistema.cenipa.fab.mil.br paginated listing via headed
  Chromium (Cloudflare JS challenge cleared), inserts rows into cenipa_reports
- fetch: downloads available PDFs (EN preferred, PT fallback) into pdf-dir;
  rows without PDF advance with pdf_path=NULL
- parse: pdftotext extracts narratives; source_tier=pdf/short/none; lang=en/pt
- build: rows with narrative >= 80 chars projected into cenipa_accidents (country=BR)

## Notes

- Headed Chromium + Xvfb is mandatory for `discover` and `fetch` (Cloudflare).
- `parse` and `build` are pure SQLite steps — no browser needed.
- On the mini-PC, `cenipa-cycle.timer` fires weekly Sun 18:00 UTC and runs
  all four stages automatically via `deploy/run-cycle.sh`.

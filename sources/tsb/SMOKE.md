# TSB Canada smoke — not yet run

This file used to be a byte-identical copy of `sources/bea/SMOKE.md`: it
recorded a **BEA France** smoke run (15 events off page ~200 of the bea.aero
global list, 4 built rows, French narratives) and was never re-run for
TSB Canada. Nothing in it described this package.

The observed results are gone rather than rewritten, because inventing numbers
for a run that did not happen is worse than admitting there is none.

## Procedure to run one

```bash
# Bounded discover into a throwaway DB, then the real stages.
.venv/bin/python -m tsb_ingest.cli discover --db smoke.db --max-pages 1
.venv/bin/python -m tsb_ingest.cli fetch    --db smoke.db --pdf-dir smoke-pdfs
.venv/bin/python -m tsb_ingest.cli parse    --db smoke.db
.venv/bin/python -m tsb_ingest.cli build    --db smoke.db

python -c "import sqlite3; c=sqlite3.connect('smoke.db'); \
  print(c.execute('SELECT status, COUNT(*) FROM tsb_reports GROUP BY status').fetchall()); \
  print(c.execute('SELECT COUNT(*) FROM tsb_accidents').fetchone())"

rm -f smoke.db smoke.db-wal smoke.db-shm && rm -rf smoke-pdfs
```

## What to record here

Counts per stage against tsb.gc.ca, how many rows built and why the rest were
skipped, and a verbatim narrative snippet proving the PDF text layer is real.
Fill this in from an actual run — not from another source's.

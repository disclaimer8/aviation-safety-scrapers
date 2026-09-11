#!/usr/bin/env bash
# Deliberately not 'set -e': a failing verb must not stop the ones after it.
# Rows discovered on an earlier run still deserve to be fetched, parsed and
# built, and a discover that raises (a zero-yield tripwire) has to be visible
# without costing the rest of the cycle. The worst exit code wins at the end.
set -uo pipefail
cd /home/a1/jcaa-ingest
. .venv/bin/activate
DB=/home/a1/jcaa-ingest/jcaa.db
PDFS=/home/a1/jcaa-ingest/pdfs
rc=0
python -m jcaa_ingest.cli discover --db "$DB" || { rc=$?; echo "[run-cycle] discover failed (rc=$rc)" >&2; }
python -m jcaa_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS" || rc=$?
python -m jcaa_ingest.cli parse    --db "$DB" || rc=$?
python -m jcaa_ingest.cli build    --db "$DB" || rc=$?
bash /home/a1/jcaa-ingest/jcaa-sync-to-prod.sh "$DB" || echo "[run-cycle] jcaa-sync failed (non-fatal)"
exit $rc

#!/usr/bin/env bash
# Deliberately not 'set -e': a failing verb must not stop the ones after it.
# Rows discovered on an earlier run still deserve to be fetched, parsed and
# built. The worst exit code wins at the end.
set -uo pipefail
cd /opt/ttcaa
. .venv/bin/activate
DB=/opt/ttcaa/ttcaa.db
PDFS=/opt/ttcaa/pdfs
rc=0
python -m ttcaa_ingest.cli discover --db "$DB" || { rc=$?; echo "[run-cycle] discover failed (rc=$rc)" >&2; }
python -m ttcaa_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS" || rc=$?
python -m ttcaa_ingest.cli parse    --db "$DB" || rc=$?
python -m ttcaa_ingest.cli build    --db "$DB" || rc=$?
exit $rc

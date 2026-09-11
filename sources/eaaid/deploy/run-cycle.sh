#!/usr/bin/env bash
# Deliberately not 'set -e': a failing verb must not stop the ones after it.
# Rows discovered on an earlier run still deserve to be fetched, parsed and
# built. The worst exit code wins at the end.
set -uo pipefail
cd /opt/eaaid
. .venv/bin/activate
DB=/opt/eaaid/eaaid.db
PDFS=/opt/eaaid/pdfs
rc=0
python -m eaaid_ingest.cli discover --db "$DB" || { rc=$?; echo "[run-cycle] discover failed (rc=$rc)" >&2; }
python -m eaaid_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS" || rc=$?
python -m eaaid_ingest.cli parse    --db "$DB" || rc=$?
python -m eaaid_ingest.cli build    --db "$DB" || rc=$?
exit $rc

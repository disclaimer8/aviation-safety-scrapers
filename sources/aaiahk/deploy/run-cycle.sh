#!/usr/bin/env bash
# Deliberately not 'set -e': a failing verb must not stop the ones after it.
# Rows discovered on an earlier run still deserve to be fetched, parsed and
# built. The worst exit code wins at the end.
set -uo pipefail
cd /opt/aaiahk
. .venv/bin/activate
DB=/opt/aaiahk/aaiahk.db
PDFS=/opt/aaiahk/pdfs
rc=0
python -m aaiahk_ingest.cli discover --db "$DB" || { rc=$?; echo "[run-cycle] discover failed (rc=$rc)" >&2; }
python -m aaiahk_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS" || rc=$?
python -m aaiahk_ingest.cli parse    --db "$DB" || rc=$?
python -m aaiahk_ingest.cli build    --db "$DB" || rc=$?
exit $rc

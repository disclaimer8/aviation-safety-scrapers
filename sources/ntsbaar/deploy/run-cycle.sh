#!/usr/bin/env bash
# Not 'set -e': a failing verb must not stop the ones after it, and discover
# raises by design when the index yields nothing. The worst exit code wins.
set -uo pipefail
cd /opt/ntsbaar
. .venv/bin/activate
DB=/opt/ntsbaar/ntsbaar.db
PDFS=/opt/ntsbaar/pdfs
rc=0
python -m ntsbaar_ingest.cli discover --db "$DB" || { rc=$?; echo "[run-cycle] discover failed (rc=$rc)" >&2; }
python -m ntsbaar_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS" || rc=$?
python -m ntsbaar_ingest.cli parse    --db "$DB" || rc=$?
python -m ntsbaar_ingest.cli build    --db "$DB" || rc=$?
exit $rc

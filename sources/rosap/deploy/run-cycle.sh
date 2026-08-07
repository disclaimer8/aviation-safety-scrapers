#!/usr/bin/env bash
# Not 'set -e': a failing verb must not stop the ones after it, and discover
# raises by design when the listing yields nothing. The worst exit code wins.
set -uo pipefail
cd /opt/rosap
. .venv/bin/activate
DB=/opt/rosap/rosap.db
PDFS=/opt/rosap/pdfs
rc=0
python -m rosap_ingest.cli discover --db "$DB" || { rc=$?; echo "[run-cycle] discover failed (rc=$rc)" >&2; }
python -m rosap_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS" || rc=$?
python -m rosap_ingest.cli parse    --db "$DB" || rc=$?
python -m rosap_ingest.cli build    --db "$DB" || rc=$?
exit $rc

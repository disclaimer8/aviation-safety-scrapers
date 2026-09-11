#!/usr/bin/env bash
# Deliberately not 'set -e': a failing verb must not stop the ones after it.
# Rows discovered on an earlier run still deserve to be fetched, parsed and
# built. The worst exit code wins at the end.
set -uo pipefail
cd /opt/aaicnp
. .venv/bin/activate
DB=/opt/aaicnp/aaicnp.db
PDFS=/opt/aaicnp/pdfs
rc=0
python -m aaicnp_ingest.cli discover --db "$DB" || { rc=$?; echo "[run-cycle] discover failed (rc=$rc)" >&2; }
python -m aaicnp_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS" || rc=$?
python -m aaicnp_ingest.cli parse    --db "$DB" || rc=$?
python -m aaicnp_ingest.cli build    --db "$DB" || rc=$?
exit $rc

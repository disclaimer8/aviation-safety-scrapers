#!/usr/bin/env bash
set -uo pipefail
cd /opt/ovv
. .venv/bin/activate
DB=/opt/ovv/ovv.db

# discover() now raises when a listing page fails or when page 1 yields no
# links, instead of quietly treating either as the end of the listing. That
# signal must be loud, but it must not also block the rest of the cycle: rows
# discovered on earlier runs still deserve to be fetched and built. So the
# failure is recorded and re-raised at the end, after the other two verbs run.
rc=0
python -m ovv_ingest.cli discover --db "$DB" || {
  rc=$?
  echo "[run-cycle] discover failed (rc=$rc) — continuing with fetch/build" >&2
}
python -m ovv_ingest.cli fetch --db "$DB" --pdf-dir /opt/ovv/pdfs || rc=$?
python -m ovv_ingest.cli build --db "$DB" || rc=$?
exit "$rc"

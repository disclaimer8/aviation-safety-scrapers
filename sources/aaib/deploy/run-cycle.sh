#!/usr/bin/env bash
set -euo pipefail
cd /opt/aaib
. .venv/bin/activate
DB=/opt/aaib/aaib.db
PDFS=/opt/aaib/pdfs
python -m aaib_ingest.cli discover --db "$DB"
python -m aaib_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m aaib_ingest.cli parse    --db "$DB"
python -m aaib_ingest.cli build    --db "$DB"

# Project the freshly-built local aaib.db into prod (scp + ATTACH
# projection + occurrences rebuild + nginx purge). set -e aborts the
# cycle if the sync fails, leaving prod untouched; data is safe in the
# local aaib.db for the next run. Mirrors baaa-cycle.sh final sync.

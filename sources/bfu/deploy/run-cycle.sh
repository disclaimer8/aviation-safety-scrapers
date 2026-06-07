#!/usr/bin/env bash
set -euo pipefail
cd /opt/bfu
. .venv/bin/activate
DB=/opt/bfu/bfu.db
PDFS=/opt/bfu/pdfs
python -m bfu_ingest.cli discover --db "$DB"
python -m bfu_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m bfu_ingest.cli parse    --db "$DB"
python -m bfu_ingest.cli build    --db "$DB"

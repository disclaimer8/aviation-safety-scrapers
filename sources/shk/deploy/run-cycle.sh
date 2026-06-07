#!/usr/bin/env bash
set -euo pipefail
cd /opt/shk
. .venv/bin/activate
DB=/opt/shk/shk.db
python -m shk_ingest.cli discover --db "$DB"
python -m shk_ingest.cli fetch    --db "$DB" --pdf-dir /opt/shk/pdfs
python -m shk_ingest.cli build    --db "$DB"

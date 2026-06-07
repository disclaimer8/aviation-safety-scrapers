#!/usr/bin/env bash
set -euo pipefail
cd /opt/taic
. .venv/bin/activate
DB=/opt/taic/taic.db
python -m taic_ingest.cli discover --db "$DB"
python -m taic_ingest.cli fetch    --db "$DB" --pdf-dir /opt/taic/pdfs
python -m taic_ingest.cli build    --db "$DB"

#!/usr/bin/env bash
set -euo pipefail
cd /opt/sust
. .venv/bin/activate
DB=/opt/sust/sust.db
python -m sust_ingest.cli discover --db "$DB"
python -m sust_ingest.cli fetch    --db "$DB" --pdf-dir /opt/sust/pdfs
python -m sust_ingest.cli build    --db "$DB"

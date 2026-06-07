#!/usr/bin/env bash
set -euo pipefail
cd /opt/aaibmy
. .venv/bin/activate
DB=/opt/aaibmy/aaibmy.db
python -m aaibmy_ingest.cli discover --db "$DB"
python -m aaibmy_ingest.cli fetch    --db "$DB" --pdf-dir /opt/aaibmy/pdfs
python -m aaibmy_ingest.cli build    --db "$DB"

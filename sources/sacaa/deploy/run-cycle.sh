#!/usr/bin/env bash
set -euo pipefail
cd /opt/sacaa
. .venv/bin/activate
DB=/opt/sacaa/sacaa.db
python -m sacaa_ingest.cli discover --db "$DB"
python -m sacaa_ingest.cli fetch    --db "$DB" --pdf-dir /opt/sacaa/pdfs
python -m sacaa_ingest.cli build    --db "$DB"

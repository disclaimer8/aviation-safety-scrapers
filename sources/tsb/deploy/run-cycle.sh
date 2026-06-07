#!/usr/bin/env bash
set -euo pipefail
cd /opt/tsb
. .venv/bin/activate
DB=/opt/tsb/tsb.db
python -m tsb_ingest.cli discover --db "$DB"
python -m tsb_ingest.cli fetch    --db "$DB"
python -m tsb_ingest.cli build    --db "$DB"

#!/usr/bin/env bash
set -euo pipefail
cd /opt/ovv
. .venv/bin/activate
DB=/opt/ovv/ovv.db
python -m ovv_ingest.cli discover --db "$DB"
python -m ovv_ingest.cli fetch    --db "$DB" --pdf-dir /opt/ovv/pdfs
python -m ovv_ingest.cli build    --db "$DB"

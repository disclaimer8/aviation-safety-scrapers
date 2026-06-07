#!/usr/bin/env bash
set -euo pipefail
cd /opt/knkt
. .venv/bin/activate
DB=/opt/knkt/knkt.db
python -m knkt_ingest.cli discover --db "$DB"
python -m knkt_ingest.cli fetch    --db "$DB" --pdf-dir /opt/knkt/pdfs
python -m knkt_ingest.cli build    --db "$DB"

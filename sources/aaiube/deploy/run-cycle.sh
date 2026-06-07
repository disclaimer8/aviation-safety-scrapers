#!/usr/bin/env bash
set -euo pipefail
cd /opt/aaiube
. .venv/bin/activate
DB=/opt/aaiube/aaiube.db
python -m aaiube_ingest.cli discover --db "$DB"
python -m aaiube_ingest.cli fetch    --db "$DB" --pdf-dir /opt/aaiube/pdfs
python -m aaiube_ingest.cli build    --db "$DB"

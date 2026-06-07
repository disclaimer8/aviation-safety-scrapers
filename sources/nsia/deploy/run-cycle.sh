#!/usr/bin/env bash
set -euo pipefail
cd /opt/nsia
. .venv/bin/activate
DB=/opt/nsia/nsia.db
python -m nsia_ingest.cli discover --db "$DB"
python -m nsia_ingest.cli fetch    --db "$DB" --pdf-dir /opt/nsia/pdfs
python -m nsia_ingest.cli build    --db "$DB"

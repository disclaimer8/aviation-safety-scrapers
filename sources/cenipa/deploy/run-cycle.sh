#!/usr/bin/env bash
set -euo pipefail
cd /opt/cenipa
. .venv/bin/activate
DB=/opt/cenipa/cenipa.db
PDFS=/opt/cenipa/pdfs
# discover + fetch need a real browser (Cloudflare) -> headed Chromium under Xvfb
xvfb-run -a python -m cenipa_ingest.cli discover --db "$DB"
xvfb-run -a python -m cenipa_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m cenipa_ingest.cli parse --db "$DB"
python -m cenipa_ingest.cli build --db "$DB"

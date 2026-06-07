#!/usr/bin/env bash
set -euo pipefail
cd /opt/ttsb
. .venv/bin/activate
DB=/opt/ttsb/ttsb.db
python -m ttsb_ingest.cli discover --db "$DB"
python -m ttsb_ingest.cli fetch    --db "$DB" --pdf-dir /opt/ttsb/pdfs
python -m ttsb_ingest.cli build    --db "$DB"

#!/usr/bin/env bash
export OCR_REMOTE="${OCR_REMOTE:?set OCR_REMOTE to user@host — redacted from the public archive}"
set -euo pipefail
cd /home/a1/aaid-ingest
. .venv/bin/activate
DB=/home/a1/aaid-ingest/aaid.db
PDFS=/home/a1/aaid-ingest/pdfs
# ⚠️ The AAID cert is pinned (aaid_ca_bundle.pem) with ONLY expiry disabled.
# If the host rotates its leaf cert, make_ssl_context() will fail loudly and the
# cycle aborts (set -e) — re-capture the bundle, do NOT disable verification.
python -m aaid_ingest.cli discover --db "$DB"
python -m aaid_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m aaid_ingest.cli parse    --db "$DB"
python -m aaid_ingest.cli build    --db "$DB"
# Phase 2: sync freshly-built aaid.db into prod (mirror cenipa recipe).
bash "$HOME/aaid-sync-to-prod.sh" "$DB" || echo "[run-cycle] aaid-sync failed (non-fatal)"

#!/usr/bin/env bash
# run-cycle.sh — nightly ingest cycle for eaaid (Egypt ECAA).
# Run from minipc as user a1.
# PDFs are fetched from Hetzner egress (site times out from Mac/minipc).
# OCR_REMOTE is set to trigger remote OCR for scanned PDFs.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
DB="$REPO_DIR/eaaid.db"
PDF_DIR="$REPO_DIR/pdfs"
export OCR_REMOTE="${OCR_REMOTE:?set OCR_REMOTE to user@host — redacted from the public archive}"

# Activate venv if present
if [ -f "$REPO_DIR/.venv/bin/activate" ]; then
    source "$REPO_DIR/.venv/bin/activate"
fi

echo "[eaaid run-cycle] $(date -u +%Y-%m-%dT%H:%M:%SZ)"

python3 -m eaaid_ingest.cli all \
    --db "$DB" \
    --pdf-dir "$PDF_DIR" \
    --ocr-remote "$OCR_REMOTE"

echo "[eaaid run-cycle] done at $(date -u +%Y-%m-%dT%H:%M:%SZ)"

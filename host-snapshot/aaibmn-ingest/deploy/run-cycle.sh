#!/usr/bin/env bash
# Mongolia AAIB weekly ingest cycle (discover->fetch->parse->build) routed
# through a DE SOCKS tunnel via the hetzner server.
#
# The Mongolia host (aaib.gov.mn) was reachable from any vantage in testing,
# but per the program's geo-route design all fetches go through Germany. The
# mini-PC has NO `hetzner` ssh alias, so the tunnel uses the IP directly with
# the mini-PC's default key (already authorized on hetzner).
set -uo pipefail

cd /home/a1/aaibmn-ingest
. .venv/bin/activate

DB=/home/a1/aaibmn-ingest/aaibmn.db
PDFS=/home/a1/aaibmn-ingest/pdfs
SOCKS=127.0.0.1:1080
HETZNER="${HETZNER:?set HETZNER to user@host — redacted from the public archive}"
PROXY="socks5://${SOCKS}"

cleanup() {
    pkill -f "ssh -fND ${SOCKS} ${HETZNER}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Open the DE SOCKS tunnel (ssh -f backgrounds itself; not a wait-loop)
pkill -f "ssh -fND ${SOCKS} ${HETZNER}" >/dev/null 2>&1 || true
ssh -fND "${SOCKS}" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 \
    -o ServerAliveInterval=30 "${HETZNER}"
sleep 2

# Verify the tunnel reaches Mongolia before proceeding
CODE=$(curl -s -m 30 --socks5-hostname "${SOCKS}" -A "Mozilla/5.0" \
    -o /dev/null -w "%{http_code}" "https://aaib.gov.mn/en/c/report?page=1" || echo 000)
if [ "${CODE}" != "200" ]; then
    echo "[aaibmn run-cycle] DE tunnel check failed (code=${CODE}); aborting" >&2
    exit 1
fi
echo "[aaibmn run-cycle] DE tunnel up (Mongolia ${CODE})"

python -m aaibmn_ingest.cli discover --db "$DB"                       --proxy "$PROXY"
python -m aaibmn_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"     --proxy "$PROXY"
python -m aaibmn_ingest.cli parse    --db "$DB"
python -m aaibmn_ingest.cli build    --db "$DB"

# Phase 2 (prod sync) is added in a later phase, mirroring ciaiac-sync-to-prod.sh:
if [ -x "$HOME/aaibmn-sync-to-prod.sh" ]; then
    bash "$HOME/aaibmn-sync-to-prod.sh" "$DB" || echo "[run-cycle] aaibmn-sync failed (non-fatal)"
fi
# Phase 2: sync aaibmn.db into prod
bash "$HOME/aaibmn-sync-to-prod.sh" "$DB" || echo "[run-cycle] aaibmn-sync failed (non-fatal)"

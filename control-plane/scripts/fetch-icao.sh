#!/bin/sh
# fetch-icao.sh — run on the mini-PC (residential egress; real-browser TLS fingerprint passes Cloudflare).
#
# Usage: fetch-icao.sh {aia|raio} > output.html
#
# Primary method: curl with a real desktop browser User-Agent.
# Works from the mini-PC because residential egress bypasses Cloudflare's bot-challenge.
# Will NOT work from a data-centre IP (Go stdlib fetcher also blocked, for the same reason).
#
# Patchright fallback (when Cloudflare hard-challenges curl):
#   xvfb-run -a <venv-python> ~/aaiuz-ingest/aaiuz_patchright_helper.py <URL>
# This renders via headed Chrome with a persistent CF-whitelisted profile.
# See the aaiuz-ingest repo for details; no path or secrets are hardcoded here.

set -eu

AIA_URL="https://www.icao.int/safety/AIG/AIA"
RAIO_URL="https://www.icao.int/safety/regional-safety-cooperation/List-of-RAIOs-and-ICMs"

case "${1:-}" in
  aia)  URL="$AIA_URL" ;;
  raio) URL="$RAIO_URL" ;;
  *)
    echo "Usage: $0 {aia|raio}" >&2
    exit 1
    ;;
esac

UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"

TMPFILE=$(mktemp)
trap 'rm -f "$TMPFILE"' EXIT

curl -fsSL --max-time 60 -A "$UA" "$URL" > "$TMPFILE"

SIZE=$(wc -c < "$TMPFILE")
if [ "$SIZE" -lt 20000 ]; then
  echo "WARNING: output is only ${SIZE} bytes — likely a Cloudflare challenge page, not real content." >&2
  echo "Try the patchright fallback: xvfb-run -a <venv-python> ~/aaiuz-ingest/aaiuz_patchright_helper.py $URL" >&2
  exit 1
fi

cat "$TMPFILE"

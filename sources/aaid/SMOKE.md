# AAID (Kenya) ingest — smoke evidence

Source: https://aaid.transport.go.ke/final-reports  (Drupal Views table, EN)
TLS: EXPIRED leaf cert (eMudhra chain, notAfter 2025-09-22). PINNED via
`aaid_ca_bundle.pem` + OpenSSL X509_V_FLAG_NO_CHECK_TIME (expiry-only bypass;
chain + hostname verification stay ON). See aaid_ingest/aaid.py make_ssl_context().

## case_id
Intrinsic = `<REGISTRATION>-<ISO event-date>` (no real reference number exists
on the listing). Order-independent, no encounter suffix. 164/164 unique.

## Smoke run (2026-06-07, Mac)
- discover: 164 rows (matches live scout)
- fetch+parse+build of 3 recent reports → 3 accidents:
    5Y-LOL-2024-12-07  42991 chars  crash-5y-lol
    5Y-CLI-2024-08-13  30715 chars  crash-5y-cli
    5Y-LLJ-2024-08-06    402 chars  crash-5y-llj
  English text-layer PDFs; country=KE; pinned-cert download verified.
- 3 oldest reports (1976/1985) correctly SKIPPED (scanned, tier 'none').

## Tests
60 passed (incl. 5 non-bleed vs aaib/aaiu/aaiube/aaibmy/aaiib).

## Re-check each cycle
If the host renews its cert and rotates the leaf, make_ssl_context() chain
validation fails LOUDLY (set -e aborts the cycle). Re-capture the bundle:
  openssl s_client -showcerts -connect aaid.transport.go.ke:443 \
    -servername aaid.transport.go.ke </dev/null > raw.txt
  awk '/BEGIN CERT/,/END CERT/' raw.txt > aaid_ca_bundle.pem
NEVER fall back to verify=False.

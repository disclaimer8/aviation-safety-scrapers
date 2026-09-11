# AAII Sri Lanka (CAA Sri Lanka) — `aaisl` source — P1 smoke

Source: https://www.caa.lk/en/aircraft-accident-and-incidents (static, English).
Transport: httpx + browser UA, throttle 1.5–2s. System `pdftotext` for extraction.

## Live smoke (2026-06-08)
- discover: **37** report rows (all 37 accident-report PDFs on the page).
- fetch: 37/37 PDFs downloaded.
- parse: 37 processed.
- build: **31** aaisl_accidents rows.
  - skipped: 1 `foreign` (TSIB re-host), 1 `scanned` (1974 scan), 4 `none` (image-only PDFs, no text).

## case_id (intrinsic, no encounter-order suffixes)
- `<REG>-<YYYYMMDD>` when registration + date known (e.g. `4R-CAE-20260107`).
- `<REG>` when only registration known.
- `LK-<slug-of-title>` fallback for reports without a registration
  (e.g. ATC near-miss / loss-of-separation incidents).

## TSIB-rehost dedup
One listed PDF (`18_4R-ABN_21MAR2019…`, case `4R-ABN-20190321`) is actually a
**Transport Safety Investigation Bureau (Singapore)** report re-hosted by CAA
Sri Lanka. It is detected at parse time via `is_foreign_authority()` (foreign
authority signature present AND no "Civil Aviation Authority of Sri Lanka"
release line) → `source_tier='foreign'`, `status='skipped'`. It is NOT ingested,
so it does not duplicate the existing `tsib` source.

Nuance: report #23 (`4R-ADG-20120205`, SLA A340 at London Heathrow) is technically
*investigated by the UK AAIB* but *released by CAA Sri Lanka*. It carries the SL
release line, so it is correctly KEPT as a genuine AAII publication. (The SL
release phrase is whitespace-tolerant because pdftotext often splits it across a
newline.)

## TLS chain (shipped CA bundle)
www.caa.lk serves an INCOMPLETE chain: the Sectigo "R36" intermediate and its
"R46" root are neither sent by the server nor present in common trust stores, so
plain curl AND the default certifi bundle fail ("unable to get local issuer
certificate") on both Mac and the mini-PC. Fix: the missing R36 intermediate +
R46 root are shipped in `aaisl_ingest/caa_lk_bundle.pem` (certifi roots + both
Sectigo certs) and httpx is pointed at it via `aaisl.ca_bundle()`. No verify=False
anywhere in the shipped code. (Cf. the AAIU TLS-intermediate trap.)

## Tests
`pytest -q` → 55 passed (incl. 4 source-key non-bleed tests vs aaib/aaiu/aaiib/aaiube/aaid).

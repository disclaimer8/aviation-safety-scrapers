# AAIIB (Philippines) ingest — P1 smoke

Source: CAAP / Aircraft Accident Investigation and Inquiry Board
Index:  https://www.caap.gov.ph/reports/  →  /{YEAR}-accidents/  (2008..present)
country = PH ; source key = aaiib (EXACT-match only; neighbours: aaib/aaibmy/aaiu/aaiube)

## Discovery
- 18 year listing pages (2008–2025).
- Accident final-report PDFs identified by registration mark in filename
  (RP-Cxxxx / RP-Rxxxx / foreign) + accident|final-report|interim keyword,
  minus site-wide boilerplate (manuals/CSR/memos/etc).
- When several PDFs share a registration in one year, the FINAL report wins
  over plain accident wins over interim statement.
- case_id = AAIIB-{YEAR}-{REG}  (no stable id in URLs). Canonical board ref
  "AAIIB-YYYY-NNN", when present in PDF text, stored as aaiib_ref.

## Live smoke (2026-06-07)
- discover: 187 reports across all 18 years.
- fetch+parse+build (3 end-to-end):
    AAIIB-2022-HL7525   PH 2022-10-23 HL7525   123,546 chars  (Korean Air A330, final report)
    AAIIB-2023-RP-C1174 PH 2023-01-24 RP-C1174  47,392 chars  (Cessna U206F)
    AAIIB-2025-RP-C8798 PH 2025-04-12 RP-C8798  37,434 chars  (aaiib_ref AAIIB-2025-314)
  All text-layer PDFs (English), tier='pdf', site_slug = lowercased case_id.

## Tests
- 67 pytest, all green (offline fixtures + non-bleed suite).
- Non-bleed: aaiib never == aaib/aaibmy/aaiu/aaiube; tables aaiib_*; slug/case_id
  prefix aaiib- not aaib-; aaiib_ref regex rejects "AAIB-...".

## Constraints / notes
- httpx + browser UA + Referer; throttle 2.0s; 60s timeout. Plain curl 200 — no
  JS challenge / no playwright.
- web.caaplocal.ph mirror (6 PDFs, 2016–2018) sometimes serves 0 bytes →
  download() raises on empty body, row stays 'new' for retry (per-row isolation).

## Deploy
- /opt/aaiib ; venv ; pip install -e .
- systemd aaiib-cycle.{service,timer} ; OnCalendar=Sun 01:00 UTC (first free slot).
- Phase 2 prod-sync line is commented in run-cycle.sh (added later).

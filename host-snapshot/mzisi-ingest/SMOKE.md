# MzI (Slovenia) ingest — P1 smoke

Source key: **mzisi**.  Country: **SI**.  Transport via httpx + system pdftotext.

## Discovery path
gov.si search ("preiskovanje letalskih nesreč") → aviation accident/incident
investigation service under the **Ministry of Infrastructure and Energy**:

  https://www.gov.si/drzavni-organi/ministrstva/ministrstvo-za-infrastrukturo-in-energetiko/o-ministrstvu/sluzbe-za-preiskovanje-letalskih-pomorskih-in-zelezniskih-nesrec-in-incidentov/sluzba-za-preiskovanje-letalskih-nesrec-in-incidentov/

This is ONE server-rendered page (~79 KB, HTTP 200, plain UA, no JS challenge)
that links directly to all report PDFs as assets under
`/assets/ministrstva/MzI/porocila-o-letalskih-nesrecah/YYYY/...pdf` (2006-2025).

Earlier stale candidate URLs (gob 301→404 under "ministrstvo-za-okolje-in-prostor")
were redirects to a dead path; the live home is under MzIE, found via gov.si search.

PDF asset types in the archive (filename keyword → handling):
  - Koncno-porocilo / KONCNO POROCILO / Koncna-porocila → FINAL report  (KEEP)
  - Povzetek(-koncnega)                                  → SUMMARY of final (KEEP)
  - Uvodno-porocilo                                       → PRELIMINARY  (skip)
  - Obvestilo / OBVEST                                    → closure notice (skip)

`parse_index()` keeps only final reports + summaries (69 of 94 PDFs).

## case_id
- staging key (PRIMARY KEY, discover time): `mzisi-YYYY-<filename-slug>` — the URL
  has no official number.
- official document number lives INSIDE the PDF text, shape `3720X-N/YYYY[-2430-NN]`
  (HAS A SLASH); extracted at parse into `source_event_id` (raw, slash preserved).
  `_normalize_case_id` collapses whitespace around `-`/`/`.
- build() emits accident rows keyed on `source_event_id` when present, else the
  staging case_id. site_slug = lowercased `[^a-z0-9]+`→`-`.

## Live smoke (2026-06-08)
1. discover → **69** final reports (years 2006-2024; scout's ~28 was an undercount).
2. fetch+parse+build on 4 sampled reports → fetched 4, parsed 4, **built 3**:
   - 37200-6/2016-2430-39  lang=sl  tier=pdf  nlen=63278  reg=S5-DES   slug=crash-s5-des
   - 37200-3/2020          lang=sl  tier=pdf  nlen=44218  reg=S5-PGC2  slug=crash-s5-pgc2
   - 37200-7/2024-2430-61  lang=sl  tier=pdf  nlen=77966  reg=None     slug=crash-mzisi
   - 2006 Cameron-Z-120 → scanned/image-only PDF (0 extractable chars) → **skipped**
     (scanned-aware floor working as intended).
   All sampled finals were **Slovenian** (sl) — the predominant language of the
   final-report set; P3 will handle SL→EN.  detect_lang() tags 'en' when English
   markers outweigh Slovenian.
3. pytest: **49 passed**.

## Notes / concerns
- Reports are overwhelmingly Slovenian; English finals are rare (scout's
  "many English" did not hold for the final-report set). lang is detected per PDF.
- Best-effort `registration` comes from the filename; underscore-delimited names
  (e.g. `Cessna-172_S5-DLM`) miss it → site_slug falls back to crash-mzisi. The
  case_id (from PDF) is the authoritative join key; reg can be enriched downstream.
- Older PDFs (notably 2006-2009 gliders/balloons) are scanned images → skipped by
  the scanned/none floor; ~the post-2012 set carries clean text.

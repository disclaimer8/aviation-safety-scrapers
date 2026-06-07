# RNSA Iceland (aviation) ingest — smoke results (2026-06-05, Mac)

```
discover (full): 71 reports
  walked /flug/slysa-og-atvikaskyrslur/{YEAR}/ for 2009..(current+1);
  2025/2026/2027 → HTTP 404 (unpublished) skipped gracefully.
  by year: 2009=1 2010=1 2011=5 2012=3 2013=8 2014=9 2015=4 2016=3
           2017=4 2018=7 2019=9 2020=4 2021=2 2022=3 2023=2 2024=6
  by kind: Final=57  Interim=3  (None=11 → filenames w/o loka/final/interim kw)
  by lang: is=52  en=19   (filename keyword + PDF-text stopword sniff)
  with TF-registration=57 · with parseable filename date=32 · with ICAO=19
fetch+build (sample, 6 reports across eras incl. one English-named + interim):
```

| case_id | year | report_type | reg | lang | tier | narrative | icao |
|---|---|---|---|---|---|---|---|
| 1160 | 2010 | Final   | TF-KEX | is | pdf | 30,575 ch | — |
| 3714 | 2014 | Final   | TF-142 | en | pdf | 4,911 ch  | — |
| 4816 | 2017 | Final   | TF-FIP | en | pdf | 49,495 ch | — |
| 4707 | 2019 | Final   | TF-MAJ | en | pdf | 7,698 ch  | — |
| 4998 | 2022 | Interim | TF-ABB | is | pdf | 10,569 ch | — |
| 5312 | 2024 | (none)  | TF-NLA | en | pdf | 14,146 ch | BGNO |

All narratives have clean text layers (well over the 300-char floor). TF-
registration extraction VERIFIED across eras (filename-first, PDF-text
fallback). Language detection VERIFIED: filename keyword guess, then a
stopword sniff of the actual PDF text confirms/overrides — e.g. 5312
(`tf-nla-loss-of-cabin-pressure-bgno`, no kind keyword) is re-classified `en`
from its English body. event_date fallback VERIFIED: 5312 / 4998 carry no
parseable filename date → `{year}-01-01`. ICAO token VERIFIED: 5312 → `BGNO`.

## Deviations from scout

- **2025 archive page is 404 (not yet published)** → live total is **71 across
  2009-2024**, not "2009-2025". The scout's "~71" estimate is exactly right; it
  just mis-attributed some to 2025. The walker probes current-year+1, so 2025
  is auto-picked the moment RNSA publishes it.
- No form/notification PDFs were actually present in any 2009-2024 year-page
  item block (the `tilkynning`/`eyðublað` filter is in place and tested, but
  fired on zero live rows — every listed PDF was a genuine report).
- Listing is RICHER than scout noted: each report sits in a
  `<div class="item">` with an `<h3>` title + summary `<p>`, both captured
  (title/summary stored on `rnsa_reports`) — not filename-only.

## Source shape (verified live)

- PER-YEAR archive pages (no hub):
  `https://rnsa.is/flug/slysa-og-atvikaskyrslur/{YEAR}/`, walked
  2009..(current calendar year + 1). 404 = unpublished/future year, tolerated.
  The paginated `?page=N` view is a SUBSET and is deliberately NOT used.
  Plain ASP.NET server-rendered HTML, no anti-bot; curl/httpx + browser UA →
  HTTP 200, clean under httpx+certifi (no TLS quirk).
- Each report = one `<div class="item">` block: `<h3>` title +
  summary `<p>` + a single `<a href="/media/{id}/{slug}.pdf">Skýrsla</a>`
  (1:1 item↔PDF, verified across all 16 live years). Link text is generic
  ("Skýrsla") so metadata is parsed best-effort from the rich FILENAME + h3.
- PDFs at `/media/{id}/{slug}.pdf`, strong text layers (4.9K-49.5K chars
  observed; legacy 2010 still clean). pdftotext clean. Mixed Icelandic
  (`lokaskyrsla…`) and English (`final-report-…`, `interim-report-…`)
  filenames + bodies.
- FILENAME metadata: registration(s) `TF-[A-Z0-9]{3}` (sometimes TWO, e.g.
  `tf-dro-og-tf-kfb`), airport ICAO `BI**`/`BG**`, and a date in Icelandic OR
  English month names (`mai`/`agust`/`februar`/`february`, day before OR after
  the month, ordinal suffixes tolerated). Year comes authoritatively from the
  year-page URL.
- ⚠️ Some `/media` ids are notification forms / blank forms
  (`tilkynning`/`eyðublað`), filtered by `is_form_pdf`; only PDFs inside item
  blocks are taken (media ids never guessed).
- report_type: `lokaskyrsla`/`final-report` → Final;
  `bradabirgdaskyrsla`/`interim-report` → Interim; else None.
- language: filename keyword first, then a stopword sniff of the PDF text
  confirms/overrides; defaults to `is` (source-native; translate pipeline
  downstream handles `is`).
- Registration: filename-first; best-effort `TF-` recovery from the PDF text
  when the filename carried none (foreign-registered → None).

## Pipeline

discover (walk per-year archive pages 2009..current+1, tolerate 404 → parse
`<div class="item">` blocks → best-effort metadata from filename+title →
INSERT keyed on case_id = numeric `/media/{id}`, stable & unique) → fetch
(download, pdftotext, tier pdf/scanned, re-confirm registration + language from
the text layer) → build (floor 300 → rnsa_accidents, country 'IS', lang,
report_type Final/Interim, event_date from filename else `{year}-01-01`).
Throttle 1.0s.

DB: `rnsa_reports` (case_id PK = media id, pdf_url UNIQUE) + `rnsa_accidents`
(case_id PK, country DEFAULT 'IS', lang column).

## Tests

40 offline tests (no network), all green:
`pytest -q → 40 passed`. Coverage: year-page URL construction (incl.
current+1 probe), filename date parse (Icelandic + English months, both
orders, ordinals), single + DUAL TF- registration, ICAO token, report-kind,
language detection (filename + text-override + default), form-PDF filter,
event-date fallback, live-fixture year-page parse, schema/uniqueness, CLI
args, and full pipeline state machine (404 tolerance, form filter,
tier pdf/scanned, fetch-failure retry, registration recovery from PDF text,
build floor + country + idempotency).
```
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

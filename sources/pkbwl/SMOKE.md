# PKBWL Poland ingest — smoke results (2026-06-05, Mac)

```
discover (live, listing walk):
  page 1..236 = 10 reports/page; page 237 → HTTP 404 (clean past-the-end stop).
  Last real page = 236 (8 slugs) ⇒ ~235×10 + 8 = ~2,358 reports, 2004-2026.
  ⚠️ Recent pages (all-2026) are OPEN investigations with no PDF yet → 0
  inserted; reports surface once a Final/Resolution PDF is published.
fetch+build (sample: EN-variant final, PL-only resolution, older 2004/2005,
mid-range 2019):
```

| case_id | date | class | reg | lang | type | narrative |
|---|---|---|---|---|---|---|
| 2022-2456 | 2022-05-23 | POWAŻNY INCYDENT (SERIOUS INCIDENT) | SP-MMB | **en** | Final | 33,732 ch |
| 2018-0503 | 2018-03-10 | INCYDENT (INCIDENT) | SP-CEZ | **en** | Resolution | 3,423 ch |
| 2019-1816 | 2019-05-25 | WYPADEK (ACCIDENT) | SP-SCLK | pl | Final | 8,913 ch |
| 2015-1098 | 2015-06-22 | INCYDENT (INCIDENT) | (none) | pl | Resolution | 4,969 ch |
| 2005-0089 | 2005-06-05 | WYPADEK (ACCIDENT) | SP-3382 | pl | Resolution | 4,155 ch |
| 2004-0127 | 2004-06-27 | WYPADEK (ACCIDENT) | (none) | pl | Resolution | 2,923 ch |

A full live `discover` against one older listing page (slugs 2019-1810…1971)
inserted 10 reports with registration / date / class taken from the detail
page (incl. foreign marks D-EKUC, A44RFD, D-MAXK), report types correctly
classified Final / Resolution.

**Registration VERIFIED from the detail page** (inline `<dl>` metadata, never
from the PDF): SP-MMB, SP-CEZ, SP-SCLK, SP-3382 (Polish) and D-EKUC / A44RFD
(foreign), stored as-is. Reports with no registration cell yield `None`.

**EN-variant preference VERIFIED**: 2022-2456 and 2018-0503 carry an `_ENG`
PDF — chosen over the Polish sibling, stored `lang='en'`, and both extracted
**clean** (single-char-token fraction 0.037 / 0.046, far below the 0.40
degeneracy threshold). Reports without an English variant (the majority) keep
`lang='pl'`. The spaced-letter (`P R E L IM IN A RY`) EN→PL fallback is wired
and unit-tested; no degenerate EN PDF was hit in the live sample (observed EN
fractions 0.037-0.046; observed PL fractions 0.088-0.119 — all well clear of
the threshold, so the heuristic never false-fires on normal Polish typography).

## Source shape (verified live)

- ⚠️ DOMAIN: `https://pkbwl.gov.pl/` (own WordPress). NEVER `gov.pl/web/pkbwl`
  (301-redirects datacenter IPs to gov.pl root — bot trap). curl + browser-UA
  and httpx+certifi both → HTTP 200, no TLS quirk, no anti-bot.
- LISTING `https://pkbwl.gov.pl/raporty/page/N/` (page 1 = bare `/raporty/`),
  10 reports/page, **236 pages → ~2,358 reports, 2004-2026** (scout said
  ~235 pages / ~2,350 / 2012-2026; actual oldest is **2004**, not 2012). Slugs
  via `/raporty/(\d{4}-\d{3,4})/`. ⚠️ Numbering NOT contiguous (2015-1098,
  1099, 1105, …) → walk pages, never guess. Past-the-end = HTTP 404 (clean
  stop). WP REST API + sitemap 404 (disabled). `/rejestr-zdarzen/` ignored.
- DETAIL `https://pkbwl.gov.pl/raporty/{YYYY-NNNN}/`: bilingual PL/EN metadata
  as `<dl>` blocks — two `<dt>` (Polish then grey English label) + a `<dd>`
  value. Parsed: Aircraft Type, Aircraft Registration Marks, Occurrence Place,
  Occurrence Date (**already ISO** YYYY-MM-DD), Occurrence Time (LMT),
  Occurrence Class, Aircraft Operator/User, Injury Level, Investigation Status.
- DOCUMENTS (DOKUMENTY/RECORDS) are ALSO `<dl>` blocks whose `<dd>` holds the
  PDF `<a href>`(s) under `/wp-content/uploads/YYYY/MM/`:
    - Raport Wstępny / Preliminary Report → `_RW`
    - Oświadczenie Tymczasowe / Interim Statement → `_OT`
    - Raport Końcowy / **Final Report → `_RK` (canonical)**
    - Uchwała / Resolution → `_U` / `_U2`
  Each row may carry a PL and an EN file (`_EN` / `_ENG` / `_RW_ENG`). ⚠️ Hrefs
  are HARVESTED from the page, NEVER constructed — filename prefix/suffix order
  drifts (`2019_1816_RK.pdf`, `2018-0503_U_ENG.pdf`, `U_2020_3931.pdf`,
  `3432_2019_U-1.pdf`). report_type is read from the row LABEL, not the
  filename, so messy names don't break classification.
- ⚠️ NOT every report has a Final (`_RK`); many have only a Resolution
  (`_U`/`_U2`) — we then take that (report_type='Resolution'). Open/recent
  reports have no PDF at all → skipped at discover.

## Pipeline

discover (walk `/raporty/page/N/` until 404 → for each NEW slug GET the detail
page → parse `<dl>` metadata + harvest DOCUMENTS hrefs → pick preferred
narrative: Final > Interim > Preliminary > Resolution, EN variant preferred
within the type → INSERT keyed on case_id = slug `YYYY-NNNN`; no-PDF reports
skipped) → fetch (download chosen PDF, `pdftotext`; if the EN variant extracts
degenerate/letter-spaced, re-fetch the PL sibling and keep that, lang='pl';
tier pdf/scanned) → build (floor 300 → pkbwl_accidents, country 'PL', lang =
variant kept, report_type Final/Interim/Preliminary/Resolution). DELAY 1.2s
(biggest source of the wave — polite).

DB: `pkbwl_reports` (case_id PK, pdf_url UNIQUE) + `pkbwl_accidents`
(case_id PK, country DEFAULT 'PL').

## Deviations from scout

- Oldest report year is **2004** (last page 236), not 2012 — total range
  2004-2026, ~2,358 reports.
- Listing numbering uses **4-digit** suffixes throughout (`YYYY-NNNN`), incl.
  older years (e.g. 2005-0089); slug regex `\d{3,4}` covers both.
- Observed report-type universe: `_RK` (Final), `_RW` (Preliminary), `_OT`
  (Interim), `_U`/`_U2` (Resolution). Resolution-only reports are common; the
  scout's `_OT` is rare in samples. report_type is taken from the DOCUMENTS
  row label, which is more robust than the drifting filename suffixes.
- No spaced-letter EN PDF was encountered in the live sample (EN PDFs fetched
  were clean); the density fallback is implemented + unit-tested, threshold
  0.40 sits safely above observed clean fractions (PL up to 0.12, EN ~0.04).

## Tests

37 offline tests (no network), all green:
`pytest -q → 37 passed`. Covers: listing slug regex (dedupe + ignore
pagination), own-domain guard, page-1-bare URL, 404 stop in discover, detail
`<dl>` metadata parse incl. registration (SP- and foreign), no-PDF report
skip, PDF lang classification, Final+EN preference, PL-only resolution,
EN→PL spaced-letter density fallback, db PK/UNIQUE/country-default, build
floor + idempotency, CLI args.

# DGAC Chile ingest — smoke results (2026-06-04, Mac)

```
discover (full): 230 reports  (7 GETs: the 7 hardcoded year pages 2019-2025)
  by year: 2019=28 2020=22 2021=36 2022=40 2023=30 2024=39 2025=35
  by kind: Final=77  Preliminar=92  (None=61 → bare-numbered/30-dias filenames)
fetch+build (sample, one recent Final + several older + a Chilean-reg case):
```

| case_id | year | report_type | reg | tier | narrative | aircraft |
|---|---|---|---|---|---|---|
| 2044-24 | 2024 | Final | (foreign) | pdf | 44,103 ch | TRUSH S2R-T660 (710P) |
| 2047-24 | 2024 | Final | **CC-PHQ** | pdf | 107,402 ch | ROBINSON R 44 II |
| 1976-22 | 2022 | Final | (foreign) | pdf | 125,334 ch | CESSNA 172S |
| 1972-22 | 2022 | Final | (foreign) | pdf | 19,074 ch | BOEING 787 / 767 |

All narratives are native SPANISH with clean text layers (>>300-char floor and
the >5K smoke target). **CC- registration extraction VERIFIED**: 2047-24 →
`CC-PHQ` pulled best-effort from the PDF text (the listing never carries it).
Foreign-registered aircraft (e.g. 2044-24, a Spanish-registered Thrush) yield
`registration=None` — expected.

⚠️ Scanned-tier handling verified live: 2019 case `1900-19`
(`Informe-Final-1900.pdf`) has NO text layer → tiered `scanned`, skipped by the
narrative floor (not built). This is correct behaviour for image-only legacy
PDFs.

## Source shape (verified live)

- PER-YEAR PAGES (no hub): a HARDCODED list of 7 URLs —
  `informes-2025` / `informes-2024` / **`informe-2023`** (⚠️ SINGULAR) /
  `informes-2022` / `informes-2021` / `informes-2020` / `informes-2019`.
  Never constructed (2023 breaks the `informes-{year}` pattern). curl +
  browser-UA → HTTP 200; verifies cleanly under httpx+certifi (no TLS quirk).
- Each page is a server-rendered `<table class="table">`:
  `Suceso` (case number NNNN) | `Fecha` (`15 ENE 2024` — Spanish month
  abbrevs ENE/FEB/…/DIC → ISO) | `Tipo aeronave` | `Lugar` | `Estado`
  (holds the PDF link(s)). ~22-40 data rows/year, 230 cases total.
- PDFs under `/wp-content/uploads/YYYY/MM/…`, SPANISH, 19K-125K chars.
  ⚠️ Filenames human-typed and drift: `Informe-final` vs `Informe-Final`,
  suffixes `-II` / `-1` / `i` / `a`, multi-stage
  `Informe-Preliminar-30-dias` → `-12-meses` → `-24-meses` → `-36-meses` →
  `Informe-Final`. Older years (2020-2021) use BARE `NNNN.pdf` with no
  keyword. Per case the PREFERRED stage is chosen:
  Final > latest Preliminar-NN-meses > Preliminar > 30-dias.
- ⚠️ CHROME FILTER: each page carries ~9 site-chrome PDFs (budget / SIG
  policy / privacy / prohibited-articles). Kept ONLY hrefs whose filename
  mentions `Informe`/`Preliminar`/`Final` OR embeds the row's case number
  (the bare-`NNNN.pdf` legacy case).
- Registration: NOT in the listing — `CC-[A-Z0-9]{2,4}` extracted
  best-effort from the PDF text layer; None for foreign-registered aircraft.

## Pipeline

discover (7 hardcoded year pages → parse table rows → pick PREFERRED staged
PDF per case → INSERT keyed on case_id `{caseNumber}-{YY}`, collision-
suffixed) → fetch (download, pdftotext, extract CC- registration, tier
pdf/scanned) → build (floor 300 → dgaccl_accidents, country 'CL',
report_type Final/Preliminar). DELAY 1.5s.

DB: `dgaccl_reports` (case_id PK, pdf_url UNIQUE) + `dgaccl_accidents`
(case_id PK, country DEFAULT 'CL').

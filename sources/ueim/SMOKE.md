# UEIM Turkey ingest — smoke results (2026-06-05, Mac)

```
discover: 49 reports  (2 GETs: TR canonical /hava-araci + EN /en/aircraft)
  TR page  (canonical) : 49
  EN page  (add-only)  :  0   ⚠️ EN listing carries NO report-PDF hrefs in raw
                              HTML (deviation from scout's "~53 + EN extras")
  total deduped        : 49
  by report_type: final=40  preliminary=1  unknown=8
  date range parsed: 2015-12-24 → 2025-12-23   (0 rows with NULL date)
  foreign regs: 9H-DFS (9h-dfs-on-rapor), EP-MNP (ep-mnp-nihai-rapor)
fetch+build (sample of 6 — mix of final/preliminary, TC- + foreign, old+recent):
```

| case_id | report_type | reg | event_date | tier | narrative |
|---|---|---|---|---|---|
| tc-jmm-hl-7792-nihai-rapor | final | TC-JMM | 2018-05-13 | pdf | 40,919 ch |
| tc-cck | **unknown** | TC-CCK | 2020-01-07 | pdf | 38,987 ch |
| tc-ajc-hava-araci-kazasi-nihai-raporuu | final | TC-AJC | 2022-05-21 | pdf | 24,680 ch |
| tc-bdj-nihai-rapor | final | TC-BDJ | 2022-06-26 | pdf | 30,330 ch |
| 9h-dfs-on-rapor | **preliminary** | **9H-DFS** | 2025-12-23 | pdf | 10,279 ch |
| ep-mnp-nihai-rapor | final | **EP-MNP** | 2015-12-24 | **scanned** | 0 ch (skipped) |

All built narratives are native TURKISH with clean text layers (10K–41K chars,
>> the 300-char floor). 5 of 6 built; **scanned-tier verified live**: the oldest
case (EP-MNP, 2015) is an image-only PDF → 0 chars → tiered `scanned`, skipped by
the narrative floor (correct for legacy scans). **Foreign-reg extraction
verified**: `9H-DFS` (Malta) and `EP-MNP` (Iran) pulled from the slug prefix.
**Suffix → report_type verified**: `-on-rapor` → preliminary, `-nihai-rapor*` /
`-final-raporu` → final, bare `tc-cck` → unknown.

## Source shape (verified live 2026-06-05)

- Live entity: `https://ulasimemniyeti.uab.gov.tr` (Ulaşım Emniyeti İnceleme
  Merkezi — Transport Safety Investigation Center; aviation = successor of KAIK).
  NOT the dead `ueim.uab.gov.tr`. Next.js SSR behind nginx + Google PageSpeed;
  report links ARE in raw curl HTML (no JS). curl + browser-UA → HTTP 200; clean
  under httpx+certifi (no TLS quirk, no Cloudflare/Akamai). One transient
  connection blip seen on burst → `fetch_page`/`download_pdf` retry twice with
  backoff.
- ONE server-rendered listing, no pagination:
  - TR (canonical, richest): `https://ulasimemniyeti.uab.gov.tr/hava-araci`
  - EN (secondary): `https://ulasimemniyeti.uab.gov.tr/en/aircraft`
    ⚠️ EN page carries NO `/uploads/pages/hava-araci/*.pdf` hrefs in raw HTML
    (and none under `?PageSpeed=noscript`) — so it currently adds 0 reports. The
    add-only EN merge is wired and will pick up any EN-exclusive PDFs if they
    ever appear (those get `lang='en'`).
  - The page is 6 paginated tables (year-grouped) but every report row carries
    its metadata in labelled cells, so parsing is table-split-agnostic.
- Each report row is `<td aria-label="…">` cells:
  `KAZA TARİHİ` (accident date `DD.MM.YYYY`) | `TESCİL İŞARETİ` (registration
  e.g. `TC-ERA`) | `KAZA YERİ` (location) | `KAZA TÜRÜ` (KAZA/CİDDİ OLAY/OLAY) |
  `RAPOR TARİHİ` (report date + the PDF `<a>`). ⚠️ NO aircraft-type column —
  `aircraft` stays NULL.
- PDFs under a FLAT path
  `https://ulasimemniyeti.uab.gov.tr/uploads/pages/hava-araci/{slug}.pdf`,
  TURKISH, 10K–41K chars. Hrefs HARVESTED from the page, never constructed.
  Only hrefs under `/uploads/pages/hava-araci/` are kept (site chrome dropped).
- ⚠️ Filename suffixes inconsistent and carry the report type:
  `-nihai-rapor` / `-nihai-raporu` / `-nihai` / `-final-raporu` /
  `-nihai-rapor-karar-sayili` / `…-doc-1` → **final**; `-on-rapor` →
  **preliminary** (checked first — shares the `rapor` substring); no keyword
  (e.g. `tc-cck`) → **unknown**.
- ⚠️ Registration = filename PREFIX (also in the `TESCİL İŞARETİ` cell):
  `tc-ajc-…` → `TC-AJC`; foreign regs `9h-dfs` → `9H-DFS`, `ep-mnp` → `EP-MNP`.
  Listing cell preferred, slug-prefix fallback, PDF-text TC- re-verify.
- ⚠️ The SAME registration can belong to TWO different accidents (TC-ERA: 2023
  ISPARTA `tc-era-nihai-rapor` vs 2021 `tc-era-nihai-rapor-imzali`). Dedup is by
  PDF URL / slug, NEVER by registration.
- case_id = the PDF slug (filename stem) — unique + permanent in uploads path.

## Pipeline

discover (TR listing → parse labelled rows → harvest report PDF href → INSERT
keyed on case_id=slug; then EN listing add-only, dedup by PDF URL) → fetch
(download, pdftotext, verify TC- registration from text, tier pdf/scanned) →
build (floor 300 → ueim_accidents, country 'TR', report_type
final/preliminary/unknown, lang). DELAY 1.0s, 2 retries w/ backoff.

DB: `ueim_reports` (case_id PK, pdf_url UNIQUE) + `ueim_accidents`
(case_id PK, country DEFAULT 'TR'). `source_url` = the PDF URL.

## Deviations from scout

- EN page (`/en/aircraft`) has **0** report-PDF hrefs in raw HTML (scout
  expected EN extras). Total is **49**, all from the TR page (scout said ~53).
  The 49 TR PDFs are the full current set; EN merge stays wired for the future.
- Listing has **no aircraft-type column** (only registration). `aircraft` is
  NULL at discover; best-effort recovery from PDF text is left for downstream.
- Dates in the table are numeric `DD.MM.YYYY` (not Turkish month names);
  Turkish month-name parsing is still implemented as a fallback per scout.

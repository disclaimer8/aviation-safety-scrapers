# TTSB Taiwan ingest — smoke results (2026-06-07, Mac)

⚠️ This is TTSB (Taiwan Transportation Safety Board, ttsb.gov.tw). It is NOT
the Canadian `tsb` source — every identifier here is `ttsb_` / `ttsb-` prefixed.

```
discover (live, full): EN rows 149 | ZH rows 149 | matched 149  (10 GETs:
  EN ?Page=1..5 + ZH ?Page=1..5) → 149 reports INSERTed
  EN inline /media PDFs: 125  |  need detail-harvest (older 'More Reports'): 24
  report kinds (EN list): Executive Summary=112  Final=13  Report(detail-only)=24
  drones (B-AAA…): 3        rows carrying a reg in the listing title: 19
  date span: 1999-04-21 → 2024-12-09  (incl. inherited pre-2019 ASC archive)
```

## Sample (fetch + build, 6 reports across eras)

| case_id | event_date | reg | report_type | lang | narrative | en_summary | aircraft |
|---|---|---|---|---|---|---|---|
| ASC-AAR-00-04-001 | 1999-04-21 | B-55502 | Report | zh | 66,084 | — | MBB/Kawasaki BK117 |
| ASC-AAR-00-11-001 | 1999-08-24 | B-17912 | Final | **en** | 230,480 | — | Boeing/MD-90 |
| TTSB-AOR-24-07-001 | 2023-01-17 | **B-AAA01397** | Executive Summary | zh | 9,248 | 1,963 | UAV/AVIX AXH-E230 |
| TTSB-AOR-25-07-001 | 2024-08-24 | B-88003 | Executive Summary | zh | 45,266 | 7,327 | Diamond/DA-40NG |
| TTSB-AOR-25-11-001 | 2024-11-04 | B-86002 | Executive Summary | zh | 52,409 | 5,163 | Tecnam/P2012 |
| TTSB-ASR-25-10-001 | 2024-12-09 | (none) | Executive Summary | zh | 20,135 | 5,816 | Ultra Light/Storch |

Verified live:

- **EN-summary-vs-ZH-full preference works.** Recent entries (B-86002, AFA62,
  JJ2258) ship an EN Executive Summary < 15K chars; the matching ZH PDF is the
  FULL investigation report (52K / 45K / 20K chars). The pipeline prefers the
  ZH full report as `narrative_text` (lang='zh') and keeps the EN summary in
  `en_summary_text` (5,163 / 7,327 / 5,816 chars). Confirmed by content:
  B-86002 narrative head = `國家運輸安全調查委員會…報告編號：TTSB…`, en_summary head
  = `Executive Summary  On November 4, 2024, a Tecnam P2012…`.
- **EN-full → lang='en'.** The 1999 MD-90 final (ASC-AAR-00-11-001) has a 230K
  char EN report layer (≥ 15K), so it is kept as English; no ZH fetched.
  Narrative head = `Aviation Safety Council  Accident Investigation Report  ASC-AAR-00-11-001`.
- **case_id derivation chain.** All 6 upgraded from the discover-time media
  slug to the TTSB/ASC report number pulled from the PDF text (priority 1):
  `b-86002` → `TTSB-AOR-25-11-001`, `b-aaa01397` → `TTSB-AOR-24-07-001`,
  `jj2258` → `TTSB-ASR-25-10-001`, etc. (priority 2 media-slug and priority 3
  `ttsb-{detailId}` are the fallbacks when no report number surfaces.)
- **Drone class.** B-AAA01397 captured as a `B-AAA…` registration (UAV).
- **B- registrations** verified: B-55502, B-17912, B-88003 (recovered from the
  ZH text when the listing carried no reg), B-86002, B-AAA01397.
- **Detail-harvest path.** The 24 older 'More Reports' rows have no inline
  `/media` link in the list cell; their PDF is harvested from the detail
  `/post` page. All 24 resolved (discover inserted 149/149, none dropped).

## Source shape (verified live)

- Umbraco CMS, server-rendered, **NO anti-bot**. Plain curl / httpx + a browser
  User-Agent → HTTP 200.
- ⚠️ **TLS quirk.** TTSB's certificate chain lacks the Subject Key Identifier
  extension, which Python 3.13+'s default `VERIFY_X509_STRICT` now enforces →
  `ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED] … Missing Subject Key
  Identifier`. curl/browsers accept the chain. Fix in `cli._ssl_context()`:
  certifi trust store + hostname verification kept, only the strict flag
  cleared.
- **EN list** (paginated, 30 rows/page, `?Page=1..5`, 149 total):
  `/english/16051/16052/16053/16058/Lpsimplelist`
  **ZH mirror** (same 149-set, ZH PDFs are the FULL reports):
  `/1133/1154/1155/1159/Lpsimplelist`
- ⚠️ **Detail-node prefix ≠ list path.** Row detail `/post` links live under a
  DIFFERENT node prefix than the list URL: EN `/english/18609/18610/{id}/post`,
  ZH `/1243/16869/{id}/post`. Rows are matched ONLY by that detail-node prefix
  (the page is full of other `/post` chrome links, some with doubled slashes);
  never by the list's own path.
- Each row carries Date | Title (→ detail link, often the registration) |
  Aircraft Model | Location | Report (an inline `/media/{id}/….pdf`, or a
  'More Reports' link to the detail page).
- ⚠️ **ZH dates are ROC (民國) calendar** — `113-11-04` = ROC 113 → 2024-11-04
  (ROC year + 1911). EN dates are already Gregorian ISO.
- PDFs under `/media/{id}/…pdf`, directly fetchable.
- EN↔ZH rows (same 149-set) matched by (date, registration), then (date,
  aircraft model), then (date) alone — the last needed because the EN aircraft
  string ('Ultra Light/Storch') differs from the ZH one ('超輕型載具/STORCH')
  and most rows have no listing registration.

## Pipeline

discover (walk EN + ZH lists by detail-node prefix → pair EN↔ZH by date+reg →
resolve EN/ZH PDFs, harvesting the detail page when no inline `/media` →
INSERT keyed on a derived case_id) → fetch (download EN PDF; if it's a stub
< 15K and a matching ZH PDF exists, also download the ZH full report and prefer
it, keeping the EN summary; pdftotext; tier pdf/scanned; upgrade case_id from
the TTSB/ASC report number in the text) → build (floor 300 → `ttsb_accidents`,
country 'TW', lang per the chosen narrative, event_date = occurrence date,
source_url = EN detail `/post`). DELAY 1.0s.

DB: `ttsb_reports` (case_id PK, detail_id UNIQUE) + `ttsb_accidents`
(case_id PK, country DEFAULT 'TW', lang, en_summary_text).

## Tests

`pytest -q` → **45 passed** (offline fixtures: EN+ZH list parse,
detail-node-prefix matching incl. wrong-prefix-yields-nothing, ROC↔Gregorian
dates, registration civil+drone, report-kind, inline-vs-detail PDF, EN↔ZH
matching by date+reg/aircraft/date with single-consume, case_id derivation
chain + report-number upgrade, EN-summary-vs-ZH-full preference incl. the 15K
threshold boundary, discover/fetch/build state machine, scanned tier, fetch
failure stays 'new', build floor + country + zh-lang propagation, idempotency).

## Deploy

`deploy/ttsb-cycle.timer` (Sun 13:30 UTC placeholder) + `.service` +
`run-cycle.sh` (discover → fetch → build). Mirrors the
dgaccl deploy layout.

# AAIB Malaysia ingest — smoke results (2026-06-04, Mac)

```
discover (full): 97 reports  (~14 GETs: 1 hub + 13 year pages, 2014-2026)
fetch+build 2 rows (rest UPDATEd to 'skipped' first):
```

| case_id | tier | report_type | reg | narrative |
|---|---|---|---|---|
| a-08-22p | pdf | Final | 9M-SSW | 17,784 ch (Airbus Helicopter, A 08/22P) |
| si-01-24 | pdf | Final | 9M-ITX | 33,654 ch |

Both narratives are native English with clean text layers (>>5K floor).
PDF first line of a-08-22p = "AIRCRAFT ACCIDENT FINAL REPORT / A 08/22P …" —
the filename-derived case_id matches the report's own number.

## Source shape (verified live)

- HUB: `/en/aviation/reports/statistics-and-accident-report-aaib` —
  server-rendered, links to year child pages.
  ⚠️ Old years carry a literal `d` suffix in the href (2014d..2021d);
  recent years do not (2022..2026). Enumerated from the hub's actual
  hrefs, never constructed.
- YEAR pages: server-rendered, ~8-10 PDFs each (~97 EN reports total
  2014-2026).
- PDF hrefs under `/en/AAIB Statistic  Accident Report Document/{YEAR}/…`
  with literal spaces (often DOUBLE), parens, leading `1. `, `updated`,
  trailing `_`. In the served HTML these are ALREADY percent-encoded
  (`%20` / `%20%20`); the parser unquote→quotes (idempotent) and also
  rescues any rare literal-space href.
- ⚠️ BILINGUAL TRAP: some hrefs point to the Malay copy under `/my/AAIBmy…/`
  (e.g. 2024 `/my/…/SI 0224 9M-AZP`). Kept ONLY `/en/` path PDFs and
  deduped by report number so an EN+MY pair never both ingest.
- ALL metadata from the FILENAME (no per-report detail pages):
  - report number `r'\b(A|SI)[\s_-]?(\d{2})[\s_/-]?(\d{2})(P?)\b'` →
    case_id `a-08-22p` / `si-01-24` (lowercase, dash-joined, trailing `p`
    kept). Fallback when absent (date-keyed legacy like `07 July 2014.pdf`):
    slugified filename[:40]. Collision suffix `-2`.
  - registration `9M-XXX` / `PK-XXX` / `N###` / `HS-XXX` / `I-XXXX`
    (negative letter-lookahead, not `\b`, so trailing `_` is not consumed).
  - report_kind: Final / Preliminary / Interim from the filename;
    occurrence_type A→Accident, SI→Serious Incident.
- No TLS quirk: mot.gov.my verifies cleanly under httpx+certifi (unlike
  aaiu.ie), so no pinned-intermediate bundle is shipped.

## Pipeline

discover (hub → year hrefs → year pages → INSERT rows keyed on pdf_url,
parse filename metadata) → fetch (download, pdftotext, tier pdf/scanned) →
build (floor 300 → aaibmy_accidents, country 'MY'). DELAY 1.5s.

DB: `aaibmy_reports` (pdf_url PK, case_id UNIQUE) + `aaibmy_accidents`
(case_id PK, country DEFAULT 'MY').

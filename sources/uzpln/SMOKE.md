# UZPLN (Czech Republic) ingest — smoke results (2026-06-05, Mac)

```
discover (full walk): 684 reports  (paginated /zpravy-ln?page=0..69, then the
  zero-link stop page at page 70). Span 2003-2025.
  by case_id:  120 with a Číslo zprávy 'CZ-YY-NNNN'  /  564 surrogate
               'uzpln-{incidentId}' (NO report number — the overwhelming
               majority of reports, esp. pre-2020).
  with a narrative PDF: 679 / 5 detail pages carry no /pdf/ link.
fetch+build (sample across both filename eras + reg-bearing cases):
```

| case_id | event_date | report_type | reg | tier | narrative | aircraft |
|---|---|---|---|---|---|---|
| CZ-25-1428 | 2025-07-29 | Závěrečná zpráva | (none) | pdf | 32,792 ch | MAGIC M |
| CZ-23-1410 | 2023-12-07 | Závěrečná zpráva | (none) | pdf | 60,492 ch | VL-3 Evolution |
| uzpln-403  | 2014-12-31 | Závěrečná zpráva | **OK-4450** | pdf | 17,170 ch | Horkovzdušný balón BB45N |
| uzpln-131  | 2009-11-27 | Závěrečná zpráva | **OK-STE**  | pdf | 15,485 ch | Robinson R 44 Raven I |
| uzpln-99   | 2005-12-19 | Závěrečná zpráva | **OK-WDC**  | pdf |  6,857 ch | L 410 UVP-E 8D |

All narratives are native CZECH with clean text layers (>>300-char floor and
the >5K smoke target). **OK- registration extraction VERIFIED**: uzpln-403 →
`OK-4450`, uzpln-131 → `OK-STE`, uzpln-99 → `OK-WDC`, pulled best-effort from
the PDF text (the listing/detail pages never carry it). Recent reports
(CZ-25-1428, CZ-23-1410) yield `registration=None` — the OK- mark is not
present in extractable form in those PDFs; this is the expected best-effort
miss, NOT a defect.

⚠️ Two PDF filename eras both VERIFIED live end-to-end:
  * recent — human-readable WITH SPACES + Czech diacritics, e.g.
    `/pdf/202601121455-ZZ CZ-25-1428 Originál PK.pdf` and
    `/pdf/202509151242-ZZ CZ-23-1410  ULL VL 3 Pěšice.pdf` → URL-encoded at
    discover time (`%20`, `%C4%9B`, …) and download succeeds.
  * older — opaque hash / numeric, e.g. `/pdf/DjJVJ7Z4.pdf`,
    `/pdf/incident_yQVRKDcq.pdf`, `/pdf/20200309103859.pdf` → pass through.

## Source shape (verified live)

- SINGLE paginated listing (no hub): `https://uzpln.gov.cz/zpravy-ln?page=N`,
  10 rows/page, pages 0-69 carry data, 2003-2025. ⚠️ `page=0` and `page=1`
  return the SAME first page of data (off-by-one pagination) — harmless, the
  incident_id de-dupe absorbs the overlap. curl + browser-UA → HTTP 200; no
  anti-bot; verifies cleanly under httpx+certifi (old uzpln.cz 301→uzpln.gov.cz).
- ⚠️ STOP SIGNAL: pages past the end do NOT 404 — they return a constant
  ~17.4 KB chrome page with ZERO `/incident/` links. The walk stops on
  link-less pages, NOT on a status code.
- Each listing `<tr>`: Vydavatel | Datum události (ISO `YYYY-MM-DD`) | Číslo
  zprávy (`CZ-YY-NNNN`, often BLANK) | Druh zprávy (`Závěrečná zpráva` = final)
  | Místo události (location, often an ICAO like `LKNM`) | Druh provozu
  (operation) | Druh události (`Letecká nehoda` / `Vážný incident` / `Incident`)
  + a trailing `<a href="/incident/{id}">` detail link.
- Detail page `https://uzpln.gov.cz/incident/{id}` is a metadata cover sheet
  (NOT the narrative). A `<th><b>Label:</b></th> <td>value</td>` table repeats
  the listing fields (date here is DOTTED `YYYY.MM.DD`) and ADDS the aircraft
  type (`Typ letadla / SLZ`). It links the narrative PDF `<a href="/pdf/…">`.
- PDFs are CZECH, clean text layers (6.8K-60K chars in the sample). pdftotext
  extracts cleanly across both filename eras.
- Registration: NOT on the listing/detail — `OK-[A-Z0-9]{2,4}` extracted
  best-effort from the PDF text; None for foreign / non-extractable.

## case_id

`Číslo zprávy` (`CZ-YY-NNNN`, upper-cased) when present and unique; otherwise
the numeric surrogate `uzpln-{incidentId}` (the common case — 564/684). The
numeric `incident_id` is ALWAYS stored in its own UNIQUE column and is the
de-dupe key, so re-runs skip already-seen incidents without re-fetching detail
pages. Collision suffix `-2`, `-3`, … guarantees PK uniqueness.

## Pipeline

discover (walk listings to the zero-link stop signal → for each NEW incident_id
GET its detail page for the PDF href [spaces/diacritics URL-encoded] + aircraft
type → INSERT keyed on case_id) → fetch (download PDF, pdftotext, extract OK-
registration, tier pdf/scanned) → build (floor 300 → uzpln_accidents, country
'CZ', lang 'cs', report_type = Druh zprávy). DELAY 1.0s.

⚠️ HARDENING: the per-row detail GETs double the request rate and the server
occasionally answers a listing page with a transient rate-limit BLANK. A
link-less page is therefore re-fetched (`BLANK_RETRIES`, longer pause) before
being trusted; only `EMPTY_STREAK_STOP=3` CONSECUTIVE confirmed blanks halt the
walk. (A pure listing-only walk at 0.5s reaches page 69 with no blanks at all,
confirming the true end is deterministic.) `MAX_PAGES=200` is a hard ceiling.

DB: `uzpln_reports` (case_id PK, incident_id UNIQUE) + `uzpln_accidents`
(case_id PK, country DEFAULT 'CZ', lang DEFAULT 'cs').

## Deviations from the scout

- **case_id is the EXCEPTION, not the rule.** Only 120/684 reports carry a
  Číslo zprávy `CZ-YY-NNNN`; 564 have a BLANK report number → the
  `uzpln-{incidentId}` surrogate is the dominant path. The scout framed the
  surrogate as a rare fallback; live it is the majority. The numeric
  incident_id is stored on every row as planned.
- **Aircraft type IS on the detail page** (`Typ letadla / SLZ`) — captured for
  every report, no need to mine it from the PDF.
- **Listing date is ISO** (`2025-07-29`); the **detail date is DOTTED**
  (`2025.07.29`). The parser accepts both; discover prefers the detail value
  and falls back to the listing.
- **Older PDF hashes have more shapes than scouted**: not just `ecrSLXV8.pdf`
  but also `incident_yQVRKDcq.pdf` and pure-numeric `20200309103859.pdf`. All
  handled identically (opaque pass-through).
- **`page=0` == `page=1`** (off-by-one pagination duplicate) — absorbed by
  incident_id de-dupe; noted so a future maintainer doesn't read it as a bug.
- pages 0-69 = 70 data pages (~684 reports), consistent with the scout's
  "~68 pages / ~680 reports".

Tests: **34 offline tests, all green** (no network — HTML/PDF behaviour driven
by fixtures + a fake HTTP client).

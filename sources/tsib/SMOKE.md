# TSIB ingest — smoke results (2026-06-04, Mac)

```
discover --max-pages 2 → 100 rows ("100 articles"); clamp-stop fired on page 2
(every ?page=N returns identical HTML → its first PDF repeats page 1 → stop).
fetch+build 2 rows (oldest + newest; the other 98 set status='skipped' first).
```

| case_id | event_date | tier | narrative | type |
|---|---|---|---|---|
| tib-aai-cas-246 | 2025-05-19 | pdf | 28,221 ch (B737 9M-MLL runway incursion) | Incident |
| tsib-3c729680-…-f6af26dce832 | 2000-10-19 | pdf | 26,564 ch | Accident |

Both narratives > 5K chars; both country `SG`; source_url = the entry's PDF URL.

## Live findings (probed 2026-06-04 — DEVIATIONS from the brief)

- ⚠️ **`?page=N` is a NO-OP.** Every page (1, 2, 14, 15 tested) returns
  byte-identical content and the SAME 10 rendered `<a>` anchors. The brief's
  assumption that pages clamp at the *last real page* is not what happens —
  there is no server-side pagination at all. The **clamp-stop** rule (stop when
  a page's first PDF URL repeats one already seen) is the correct guard and
  fires on page 2.
- **The full ~100-item catalogue is in the page's inline Next.js/RSC JSON**, not
  just the 10 visible anchors. Each item is an escaped object with
  `\"date\":\"$D…\"`, `\"category\":\"Incident|Accident\"`, `\"title\"`,
  `\"description\"` (aircraft), `\"referenceLinkHref\"` (the PDF URL). `parse_listing()`
  parses all 100 from ONE fetch (preferred); rendered-anchor + aria-label parse
  is kept as a fallback (and is what the unit/pipeline fixtures exercise).
- **case_id regex fixed.** The brief's `T?[AI]B/AAI/[A-Z]+\.\d+` clips the
  leading `A` of the old-era `AIB/AAI/CAS.058` (matches only `IB/AAI/…`). Using
  `(?:TIB|AIB|IB)/AAI/[A-Z]+\.\d+` captures `TIB` (new), `AIB` (old), and bare
  `IB`. Verified: `TIB/AAI/CAS.246` → `tib-aai-cas-246`, `AIB/AAI/CAS.058` →
  `aib-aai-cas-058`.
- **Catalogue era is ~2000–2025**, not 2011–2026 (oldest report 19 Oct 2000).
- **Early reports predate the AAI/CAS numbering** — the 2000 report has no
  `*/AAI/*` id anywhere, so it correctly falls back to the URL UUID
  (`tsib-{uuid}`). This is expected, not a parse miss.
- PDFs are native English, clean text layers (~26–28K chars in the smoke pair).
- Old filenames carry literal spaces/parens → href taken verbatim, percent-
  encoded only at download time.
- mot.gov.sg serves a normal, fully-chained TLS cert (no intermediate-cert hack
  needed, unlike AAIU Ireland) — plain httpx + certifi works.
```
```

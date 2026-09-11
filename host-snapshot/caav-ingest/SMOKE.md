# CAAV (Vietnam) ingest — P1 smoke notes

Source: https://english.caa.gov.vn/doc/investigation-reports.htm  (ENGLISH)
PDFs: third-party CDN imgcaa.minhvujsc.com (parsed from detail-page href/iframe/embed).

## Reachability (live scout 2026-06-07, revised after review)
- Listing page 1 is server-rendered: 11 investigation-report rows (the SEED).
- ⚠️ The pager links to /doc/investigation-reports/trang-{2,3,4}.htm but those
  routes return an EMPTY JS shell to a non-JS client (no rows, and no
  data-loading JS in the shell to reverse-engineer). `?page=N` is ignored.
- ✅ HOWEVER: direct detail-page ID enumeration reaches ALL reports over plain
  httpx. Detail pages are /doc-detail/<slug>-<id>.htm where the slug is ignored
  and only the numeric <id> resolves. A genuine report id returns ~17 KB with a
  report-type og:title AND a valid imgcaa.minhvujsc.com/*.pdf; an unallocated id
  returns a ~10.4 KB empty shell with NO PDF (a reliable "not a report"
  sentinel); other ids are advisory circulars / decisions / forms (PDF present
  but a NON-report title) and are excluded by the report-type title filter.
  → discover() now uses two paths: (A) parse page-1 listing for the SEED ids,
    (B) ID-enumeration over a BOUNDED window derived from the seed
    (caav.id_window_from_seeds — anchored on the max seed id so a stale outlier
    can't blow the window up; ID_WINDOW_BACK/FWD = 40/40). Rows are kept only
    when (report-type title OR titleless) AND a CDN PDF exists; the forward scan
    stops after ID_FWD_EMPTY_STOP=8 consecutive empty shells so NEW higher ids
    are picked up cheaply over time. No playwright needed.
  → Live count: discovery now reaches ~25 report documents (was 10), incl.
    reports NOT on page 1 (e.g. VN-A870-2020-04-02, VN-B218-2021-12-19,
    VN-A639-2020-10-16). Some events have separate interim+final docs.

## case_id (intrinsic, order-independent)
- VN-<reg>-<event_date> when a VN-style registration AND an event date are
  derivable from the report title; else VN-<sha1(pdf_url)[:12]>.
- NO encounter-order suffixes. A leading VN-/VN in the reg is stripped so the
  country prefix is never doubled.
- Two documents of the SAME event (same reg+date, e.g. interim+final of VJ260)
  collapse to one case_id; first-seen (final report, earlier in listing) wins.

## Live smoke (fresh DB)
  python -m caav_ingest.cli all --db smoke.db --pdf-dir smoke-pdfs
  → discovered: ~25 report documents (was 10 before ID-enumeration).
  ID-enum reaches reports off page 1. Sample case_ids:
    VN-8650-2023-04-05 (Bell 505, ACCIDENT), VN-A639-2020-10-16 (A321),
    VN-A870-2020-04-02, VN-B218-2021-12-19, VN-A653-2022-04-02,
    VN-6A45A3610B20 (sha1 fallback, no clean reg+date in title).
  Registration is now context-aware: a flight number (VN125, VJ260) is never
  mis-extracted as the reg; "FLIGHT NUMBER VN125 AIRCRAFT VN-A870" → VN-A870.
  event_class derived from title (ACCIDENT vs SERIOUS INCIDENT).

## Detail-page PDF quirks handled
- PDFs appear via <a href>, <iframe src> OR <embed src> → all matched.
- Some filenames contain spaces / parens (e.g. "...2018 (4).pdf"); download()
  %-encodes the path. CDN requires Referer (https://english.caa.gov.vn/).

## Tests
  python -m pytest -q   →  74 passed

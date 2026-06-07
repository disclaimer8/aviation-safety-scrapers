# OTKES (Finland) ingest — SMOKE

Source: Onnettomuustutkintakeskus / Safety Investigation Authority Finland
(`turvallisuustutkinta.fi`), aviation investigations. Country = **FI**.
Offset note: 11th-style national SIA source; assign a fresh prod offset on
projection (mirror the cenipa/dgaccl recipe).

## Architecture (BROWSER-RENDER source)
The CMS is a Vue SPA backed by `.srv` polling. The year-listing **report
links** AND the labelled **detail metadata** (Tutkintanumero / Onnettomuuspäivä
/ Finnish summary) are JS-injected — absent from raw httpx HTML. So:

- **discover** drives Playwright Chromium: renders the aviation root → harvests
  year + topic listing URLs → renders each listing for detail URLs → renders
  each new detail page for metadata + summary + PDF href. All in one browser
  session, throttled `DELAY=1.0s`.
- **fetch** downloads the main report PDF over **plain httpx** (PDFs are static
  `/material/...` files — no cookies/JS) and `pdftotext`s it.
- **build** promotes parsed rows (narrative ≥ 300) into `otkes_accidents`.

Tables: `otkes_reports` (raw) + `otkes_accidents` (built). CLI:
`discover` / `fetch` / `build` / `all`, flags `--db --pdf-dir --headed
--max-listings --max-details --user-data-dir`.

## Headless vs headed — FINDING
**Headless works perfectly on Mac.** No anti-bot, no fingerprint block observed
(verified: 37 listings harvested + multi-era details + PDFs all render
headless). Default is **headless**; `--headed` / `OTKES_HEADED` forces a headed
browser, and `deploy/run-cycle.sh` runs `discover --headed` under `xvfb-run -a`
for parity with our other browser sources and resilience if a block ever
appears. `networkidle` is NEVER used (CMS polls forever) — every render uses
`wait_until="domcontentloaded"` + a DOM poll (≤12 s) for the expected nodes.

## Live smoke results (Mac, headless, 2026-06-07)

### Discovery — listing harvest
- **37 listings**: **31 year pages span 1996–2026** + 6 topic pages
  (`vanhemmattutkinnat`, `teematutkinnat`, `liikenneilmailu`, `liikelennot`,
  `sotilasilmailu`, `kuumailmapallot`).
- Year-page URL patterns confirmed: `{year}.html` (≥2014),
  `ilmailu{year}.html` (≤2013), suffix variant `2023_1.html`. All harvested
  from the root, never constructed. Rail/marine year pages (same hub) are
  rejected by the `/ilmailuonnettomuuksientutkinta/` segment check.

### Discovery — detail harvest (per-year sample)
| Listing | Year | Detail URLs |
|---|---|---|
| `2024.html` | 2024 | 5 |
| `2022.html` | 2022 | 3 |
| `ilmailu2010.html` | 2010 | 19 |
| `ilmailu2003.html` | 2003 | 10 |
| `ilmailu1998.html` | 1998 | 23 |

Estimated total ≈ 250–400 across all year+topic pages (matches the prompt).

### fetch + build sample (cross-era)
| case_id | event_date | tier | chars | reg |
|---|---|---|---|---|
| `b2003-01` | 2003-02-23 | pdf | 79,718 | OH-CAX |
| `c2003-09` | 2003-10-03 | pdf | 77,421 | — |
| `c2003-10` | 2003-12-06 | pdf | 82,826 | OH-LVH |
| `l2022-02` | 2022-04-17 | pdf | 114,566 | OH-XMA |
| `l2024-01` | 2024-08-11 | pdf | 87,545 | — |
| `l2024-02` | 2024-10-16 | pdf | 103,643 | — |
| `otkes-3d315ded` (2024 selvitys) | 2024-07-19 | summary | 7,184 | — |

All built into `otkes_accidents` (country FI, source_url = detail page). PDF
text layers are excellent (74K–114K chars). OH- registrations extracted
best-effort from PDF text / title.

## Key empirical findings / deviations from the brief
1. **Detail URLs are NOT pinned to the listing's own year folder.** ≤2013
   reports nest under `ilmailu{year}/{slug}.html`; some years aggregate under a
   suffixed folder (2022's reports live under `2023_1/l2022-_1.html`). The
   detail matcher accepts ANY slug nested one level under a `vuosittain/`
   folder rather than `/{year}/` — `year` is a hint only.
2. **Legacy `Tutkintanumero` carries a trailing class letter** (`C9/2003L`) and
   is glued (`C9/`, not `C 9/`). Normalised by dropping the trailing letter →
   `c2003-09`. Modern form `L2024-01` → `l2024-01`.
3. **Old reports omit the `Onnettomuuspäivä` label** (only `Julkaisupäivä`).
   The event date is recovered from the date-bearing detail title (e.g.
   "…Helsinki-Vantaalle 3.10.2003" → 2003-10-03).
4. **Lighter "selvitys" reports have no PDF and no Tutkintanumero.** Their
   on-page Finnish summary IS the narrative (tier `summary`); case_id falls
   back to `otkes-{8-hex of detail-url path}`. They still build when the
   summary ≥ 300 chars.
5. **PDF annexes** (`*_LIITE_N.pdf` / `*_Liite*.pdf`) are skipped; the main
   `*_Tutkintaselostus.pdf` is preferred.
6. **MCP Playwright server was unusable for scouting** (a rogue cached redirect
   to an unrelated site fired on a timer across navigations). The package uses
   its OWN Playwright instance, which is reliable headless — verified directly.

## Language / future work
FI primary; `lang='fi'`. Major cases have professional EN translations on the
parallel `/en/` site, but EN PDFs are NOT at guessable paths — **out of scope
for v1**. A later EN-enrich pass could render the `/en/` detail page per case
and store a parallel narrative.

## Tests
**41 offline pytest** (no network / no browser — browser layer fed by
`FakeBrowser`, PDF download by `FakeClient`):
- year-URL harvest incl. suffix (`2023_1`) + 2013/2014 pattern flip + rail/
  marine + nav-chrome rejection
- detail harvest incl. legacy `ilmailu{year}/` + aggregated `2023_1/` folders +
  dedup + nested-listing rejection
- `Tutkintanumero` normalize (modern, legacy, trailing-letter, in-title) +
  `otkes-` fallback determinism
- detail innerText parse (full / blank-case selvitys / legacy no-event-date)
- LIITE annex filtering + relative→absolute PDF
- pipeline discover/fetch/build (pdf / summary / scanned-fallback /
  download-failure tiers, idempotency, case_id collision suffix, build floor 300)
- db schema / PK / UNIQUE / idempotency

Run: `python -m pytest -q`

## Deploy
`deploy/` mirrors cenipa: `run-cycle.sh` (discover `--headed` under `xvfb-run`,
fetch+build plain), `otkes-cycle.service`
(oneshot), `otkes-cycle.timer` (**Sun 15:30 UTC**, placeholder). Install root:
`/opt/otkes`. `pdftotext` (poppler) required for fetch.

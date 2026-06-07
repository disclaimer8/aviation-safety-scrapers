# ARAIB South Korea ingest — smoke results (2026-06-07, from the mini-PC)

⚠️ Smoke MUST run from the mini-PC (residential) — the Mac's network could not
reach araib.molit.go.kr. The mini-PC is the proven vantage.

```
discover: 55 reports  (paginated LST.jsp walk: page1..page6 carry rows,
                       page7 yields 0 NEW idx → walk stops; ~30 s, throttle 1 s)
  pages: p1=10 p2=10 p3=10 p4=10 p5=10 p6=5  (= 55), p7 no-new → stop
  span : 2012 → 2025 publications (wider than the brief's "2019→2025"; still 55)
fetch+build (sample of 6 across eras, incl. Jeju Air HL8088):
```

| idx | case_id | reg | event_date | publish | report_type | tier | chars | title |
|---|---|---|---|---|---|---|---|---|
| 158912 | **aar1105** | HL7604 | 2011-07-28 | 2012-09-20 | Preliminary | pdf | 116,258 | Interim Report (Asiana Airlines Cargo 991) |
| 247386 | **air1906** | HL8071 | 2019-10-29 | 2021-11-25 | Final | pdf | 59,786 | Aircraft Serious Incident Report |
| 252897 | **aar2102** | HL7528 | 2021-05-28 | 2023-05-01 | Final | pdf | 50,365 | Final Report of B767-300 |
| 263344 | **araib-263344** | HL7525 | 2022-10-23 | 2025-03-13 | Final | pdf | 123,377 | Final Investigation Report HL-7525 |
| 266499 | **aar2203** | HL9678 | 2022-11-27 | 2025-12-15 | Final | pdf | 70,811 | Helicopter Crash, Tail-Rotor Loss |
| 262906 | **aar2404** | HL8088 | 2024-12-29 | 2025-01-31 | Preliminary | pdf | 6,124 | Preliminary Report of Jeju Air (HL8088) |

All narratives are native ENGLISH with clean text layers across every era
(6K–123K chars, all well over the 300-char floor; `tier=pdf` for all 6, no
scanned legacy PDFs in the sample).

**case number from the PDF synopsis VERIFIED** in all three forms:
- labelled `◦ Accident Number: AAR2404` → `aar2404` (Jeju HL8088)
- header `ARAIB/AAR2203` → `aar2203`, `ARAIB/AIR1906` → `air1906`
**case_id FALLBACK VERIFIED**: `263344` (the HL-7525 final report) carries no
ARAIB case number in its synopsis → `araib-263344` (idx stored always).

**HL- registration VERIFIED** on all 6 (title first, then PDF text): HL8088,
HL8071, HL7528, **HL7525** (dashed `HL-7525` in the listing title → normalised),
HL9678, HL7604.

**occurrence date (NOT publish date) VERIFIED**: e.g. Jeju `2024-12-29` (from
`Date & Time: ... December 29, 2024`) vs publish `2025-01-31`; AIR1906
`2019-10-29` (from `29 Oct, 2019` — D-Mon-Year abbreviated form). build falls
back to the listing publish date only when the synopsis date can't be parsed.

## Source shape (verified live from the mini-PC)

- ⚠️ **Access gate (TmaxSoft WebtoB)**: the first HTTPS GET returns an
  `HTTP/1.0 307` to the SAME URL with `Set-Cookie: TMOSHCooKie=…`. A persistent
  `httpx.Client` (`follow_redirects=True` + its cookie jar) replays the cookie
  transparently; the next request carries JSESSIONID/SCOUTER/clientid and
  returns 200. **HTTPS only** (port 80 = connection reset). After the handshake
  there is NO rate-limit. ⚠️ Cold TLS connections intermittently **reset** —
  observed 3 consecutive `[Errno 104] Connection reset by peer` before a clean
  200 on the very first request — so every fetch is wrapped in a
  retry/backoff loop (`RETRIES=4`, `BACKOFF=2.5 s`).
- **LISTING** (server-rendered JSP):
  `https://araib.molit.go.kr/USR/BORD0201/m_34591/LST.jsp?id=eaib0401`
  10 rows/page, pagination `&lcmspage=N`. ⚠️ The paginator widget renders only
  a FIXED window of page links — NOT trusted. The walk requests pages until one
  yields no NEW idx, then stops (page 7 advertised but added 0 → stop). 55 rows.
  Each row: No | Title (`<td class="tl"><a>…</a>`, often `…`-truncated, may
  embed `HL-xxxx`) | Date `YYYY.MM.DD` | Views.
- ⚠️ **`id`↔`m_` binding is STRICT**: `eaib0401` is valid ONLY under `m_34591`;
  a wrong pairing returns a ~624-byte `페이지 이동중` redirect stub. Any response
  under `TINY_STUB_BYTES` (2000) is treated as a fetch failure.
- **DETAIL** (per row):
  `…/m_34591/DTL.jsp?id=eaib0401&mode=view&idx=NNNNNN` — carries the FULL
  (untruncated) title and the PDF download link.
- **PDF**: the DTL page's link →
  `https://araib.molit.go.kr/LCMS/DWN.jsp?fold=/eaib0401/&fileName=<enc>.pdf`.
  ⚠️ `fileName` is non-uniform — human-readable
  (`HL8088+Preliminary+Report_English.pdf`, NO case number) OR url-encoded
  (`%28AIR1906%29_…pdf`). ALWAYS scraped from the DTL page, NEVER constructed.
- In-PDF **synopsis** (rich): `Accident Number` / `ARAIB/AAR…`, occurrence
  `Date & Time`, `Location` (with ICAO, e.g. RKJB), `Operator`, `Aircraft`,
  `Registration HL-xxxx`, serial. English board → `lang='en'`.

## Pipeline

3-stage source folded into the standard discover/fetch/build/all CLI:

discover (paginated LST.jsp walk, stop on no-new-idx → INSERT keyed on the
stable `idx`) → fetch (per `new` row: DTL page → DWN.jsp PDF url scraped →
download → pdftotext → synopsis extraction; case_id = normalised case number,
fallback `araib-{idx}`; tier pdf/scanned) → build (floor 300 → araib_accidents,
country `KR`, lang `en`, report_type Preliminary/Final). DELAY 1.0 s.

DB: `araib_reports` (**`idx` PK**, `dtl_url` UNIQUE, `case_id` filled at fetch)
+ `araib_accidents` (case_id PK, country DEFAULT 'KR', lang DEFAULT 'en').

deploy/: `araib-cycle.service` + `araib-cycle.timer` (Sun 12:30 UTC) +
`run-cycle.sh` (discover→fetch→build→sync). 44 offline tests (no network).

## Deviations from the brief

- **Publication span is 2012→2025**, not 2019→2025 — the board holds 55 English
  reports back to a 2012 Asiana 991 interim report. Count (55) matches exactly;
  the pipeline is era-agnostic (clean text layers verified back to 2012).
- The 3-stage DTL detail step is folded INTO `fetch` (not a separate CLI mode)
  to preserve the standard discover/fetch/build/all contract. `idx` (not
  case_id) is the reports PK because the canonical case number is only known
  after the PDF is read.
- `report_type`: ARAIB finals self-identify as "Aircraft Accident Report" /
  "Aircraft Serious Incident Report" (no "final" keyword), so a non-preliminary
  report-titled document is classified Final; "Interim"/"Preliminary" →
  Preliminary.

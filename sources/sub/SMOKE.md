# SUB Austria ingest — smoke results (2026-06-05, Mac)

```
discover (full): 231 reports  (hub → 8 categories → years|flat → report pages)
  by category:
    motorflugzeuge                 83   (year-based, 25 yrs 2001-2025)
    segelflugzeuge                 47   (year-based, 22 yrs)
    hubschrauber                   44   (year-based, 18 yrs)
    motorsegler                    29   (year-based, 15 yrs)
    heissluftballons               10   (FLAT)
    ultraleichtflugzeuge            7   (FLAT)
    fallschirme-haenge-paragleiter  7   (FLAT)
    international                    4   (FLAT)
  TOTAL 231 — exactly matches the deep scout.
```

fetch+build sample (6 representative reports across categories/eras incl. one
flat-category and old 2001 reports):

| case_id | year | report_type | reg | tier | narrative | aircraft |
|---|---|---|---|---|---|---|
| motorflugzeuge--2024--1223_airbus-a220-300_en | 2024 | Schriftlicher Vorbericht in Englisch | (anon) | pdf | 33,145 ch | Airbus A220-300 |
| motorflugzeuge--2001--1028_cessna-182_85034 | 2001 | Vereinfachter Untersuchungsbericht | (anon) | pdf | 7,915 ch | Cessna 182 |
| hubschrauber--2001--1211_hughes-269c_85035 | 2001 | Vereinfachter Untersuchungsbericht | (anon) | pdf | 8,910 ch | Hughes 269C |
| heissluftballons--20241116_schroeder-…-20250926052 | 2024 | (none — summary only) | (anon) | summary-only | 1,445 ch | Schroeder Fire Balloons |
| international--20141129_oe_lfj_fokker_dnk-1 | 2014 | Abschlussbericht | (anon) | scanned→**summary fallback** | 1,349 ch | Fokker F28 Mark 0070 |
| segelflugzeuge--2022--0724_eiriavion-oy_85294 | 2022 | Abschlussbericht | (anon) | pdf | 28,971 ch | Eiriavion Oy, Pik 20 D |
| **international--20120817_cirrus_oe_ddd_699-04_3-14-27** | 2012 | (Abschlussbericht) | **OE-DDD** | pdf | 19,445 ch | Cirrus |

All narratives are native **German** with clean text layers (mostly 8K-100K
chars; floor is 300). PDFs up to ~18 MB.

**OE- registration extraction VERIFIED** on `international--20120817_…oe_ddd…`
→ `OE-DDD` pulled best-effort from the PDF text. ⚠️ See deviation #2 below:
modern SUB reports ANONYMIZE the registration, so `None` is the *common* case,
not the exception — and the regex correctly rejects the `OESTER…` (Österreich)
false-positive because it requires the hyphen (`\bOE-[A-Z0-9]{3}\b`).

**Summary-fallback VERIFIED live**: `international--20141129_oe_lfj_fokker_dnk-1`
has a SCANNED/short PDF (text layer < 300) → build fell back to the stored HTML
summary (1,349 chars ≥ floor). This is exactly the dgaccl scanned-tier story
but with an HTML-summary safety net.

## Source shape (verified live)

- Base `https://www.bmimi.gv.at`. Hub `/sub/berichte/luftfahrt.html` → 8
  category links `/sub/berichte/luftfahrt/{cat}.html`.
- ⚠️ **GET only, NEVER HEAD** (HEAD → 302 → 403). The 404 page is ~290 KB but
  returns HTTP 404 — gate on status code only. Browser UA. Clean UTF-8, no
  anti-bot; 1.0s throttle is courtesy.
- **TWO category layouts** (branch on presence of `/{cat}/{YYYY}.html` year
  links):
  - YEAR-BASED (motorflugzeuge, motorsegler, segelflugzeuge, hubschrauber):
    category → year links (READ the list — gaps exist, never assume a range)
    → year page → report links `/{cat}/{YYYY}/{MMDD}_{aircraft}_{caseid}.html`.
  - FLAT (ultraleichtflugzeuge, heissluftballons,
    fallschirme-haenge-paragleiter, international): reports listed directly on
    the category page; slug starts with full `YYYYMMDD`.
- Report links: `a.card-link[href]` inside `li.col-12.overview-item`
  (regex on the path pattern is what we actually use; year-index links are
  excluded). Card also carries `small.card-date`, `h2.card-title-heading`,
  `p.card-text`.
- Report page (`main#content`):
  - `time[datetime="YYYY-MM-DD"]` = occurrence date (gold standard).
  - `span.title` = aircraft type (strip `&#xa0;` + flatten nested
    `<span lang=…>`).
  - `span.subtitle > abbr[title="Geschäftszahl"]` "GZ …" = GZ file number
    (e.g. `2025-0.211.836`; OLD reports use a different GZ format like
    `3.2.11`; sometimes ABSENT → None).
  - `p.abstract` = location line.
  - `<p>` siblings BETWEEN `p.abstract` and `div.infobox` = German SUMMARY
    (~1-1.4 K chars — a SUMMARY only; the `erstellt am` / report-type text
    in the infobox is correctly excluded).
  - `div.infobox a.file[href]` = report-type label + PDF link
    `/dam/jcr:{UUID}/{file}.pdf`. The `<a>` attribute order is
    `href` then `class="file"` on every page seen — the parser matches both
    orders defensively anyway.
- FULL narrative + OE- registration are **PDF-only** (fetch stage).

## case_id (PRIMARY KEY)

Derived from the full relative path: strip `/sub/berichte/luftfahrt/` prefix
and `.html` suffix, replace `/` with `--`, lowercase. Verified **unique
231/231**. The slug's trailing numeric is NON-unique and many slugs lack a
clean numeric; `_en`/`_de` suffixes are part of the slug (content is ALWAYS
German). Raw URL is stored alongside (`page_url`, UNIQUE).
Examples:
- `motorflugzeuge--2024--0330_reims-cessna-fr172f_85305`
- `international--20141129_oe_lfj_fokker_dnk-1` (no clean numeric)
- `motorflugzeuge--2024--1223_airbus-a220-300_en` (`_en` kept in id)

## Pipeline

discover (hub → categories → year|flat branch → GET each report-detail page →
parse metadata + HTML summary → INSERT keyed on path-derived case_id) → fetch
(download PDF, pdftotext, OE- reg best-effort, tier pdf/scanned; reports with
no PDF tier 'summary-only') → build (narrative = PDF text if ≥ 300, else HTML
summary if ≥ 300, else skip → sub_accidents, country 'AT', lang 'de',
report_type Abschluss/Untersuchungs/Vereinfachter…). DELAY 1.0s.

DB: `sub_reports` (case_id PK, page_url UNIQUE, summary_text stored so the
build fallback is possible) + `sub_accidents` (case_id PK, country DEFAULT
'AT', lang DEFAULT 'de').

## ⚠️ Deviations / additions vs the scout

1. **Registration is broadly ANONYMIZED**, not merely absent for foreign
   aircraft. Modern SUB PDFs redact the OE- tail; `None` is the *normal*
   outcome. OE- is extracted only when a report leaves it un-redacted (mostly
   older / international reports, e.g. the 2012 Cirrus `OE-DDD`). Behaviour is
   correct (best-effort, None on absence) — flagging because the *frequency*
   of None is much higher than "foreign-registered only".
2. **Extra report-type label seen in the wild:**
   `Schriftlicher Vorbericht` (and `… in Englisch`) — an advance/preliminary
   written report, present on 2024 large-airliner cases (A320, A220). It is
   NOT one of the three canonical kinds. The parser's fallback returns the
   leading phrase before the „aircraft“ quote, so it is captured verbatim
   (e.g. `Schriftlicher Vorbericht in Englisch`) rather than dropped. Only
   `Zwischenbericht` is explicitly dropped (→ None) per the scout. If the
   downstream project wants to treat `Schriftlicher Vorbericht` as
   non-final/interim, add it to `_DROP_KINDS` in `sub.py`.
3. **Old GZ format** differs (`3.2.11`, `2023-0365.910` without the dot after
   `0`) — the GZ parser keeps whatever follows the `GZ` token, so these are
   preserved rather than lost.
4. Hub link order returned by the live site is not the scout's order
   (international first) — purely DOM order; `CATEGORIES` is informational and
   the pipeline walks whatever the hub returns. Counts are identical.

## Tests

`pytest`: **38 passed**, fully offline (HTML fixtures + fake HTTP client; real
live report pages `report_recent.html` / `report_old.html` captured as
fixtures for the summary-between-abstract-and-infobox + nested-lang-span +
old-VUB cases). No network in the suite.

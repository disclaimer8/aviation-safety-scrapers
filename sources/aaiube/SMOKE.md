# AAIU Belgium ingest — smoke results (2026-06-04, Mac)

```
discover (full page GET): 164 rows
  modern AAIU-ref filenames → 29   (case_id 'aaiu-YYYY-MM-DD-NN')
  legacy filenames          → 135  (case_id 'be-{year}-{slug}')
  rows with a parsed DD/MM/YYYY date: 126 / 164
```

Fetch + build of two representative rows (one modern, one legacy):

| case_id | tier | narrative | lang |
|---|---|---|---|
| aaiu-2022-09-12-01 | pdf | 29,539 ch (Grumman AA-5B Tiger, Brussels FIR) | en |
| be-2009-2009-08    | pdf | 14,878 ch (AVRO RJ100 OO-DWK, 27 Oct 2009)    | en |

Both built into `aaiube_accidents` with country='BE'; narratives are genuine
"Air Accident Investigation Unit (Belgium) — Safety Investigation Report"
English text with clean PDF text layers (well above the 3K / 300-char floor).

## Notes / observations

- ONE server-rendered page:
  `https://mobilit.belgium.be/en/aviation/accidents-and-incidents/safety-investigations-and-reports`.
  Plain `curl` + browser UA → HTTP 200, ~90 KB. No anti-bot. TLS chain
  verifies with stock certifi (no pinned-intermediate hack, unlike Ireland).
- Markup = an accordion: one `<h3>Reports occurrences YYYY</h3>` heading per
  year, each followed by a `<table>` with columns
  *Date of occurrence | Type of aircraft | Casualties | Location | Status*.
  Cells are sometimes bare, sometimes `<p>`-wrapped; some rows carry an extra
  trailing "safety recommendations" cell (observed 5 / 6 / 7 cells per row).
  21 tables, 2006–2026.
- The report PDF is the FIRST `<a href="…​.pdf">` in the row's Status cell.
  Rows whose Status is plain text ("In progress", "Delegated to NL",
  "Progress statement" with no link) have no PDF and are dropped at discover —
  180 data rows → 164 PDF-bearing rows.
- PDFs are relative `/sites/default/files/…` and absolutised to
  `https://mobilit.belgium.be/…`; spaces percent-encoded
  (e.g. `AAIU-2022-09-08-02 final.pdf` → `…%20final.pdf`). Some `_0` / re-upload
  suffixes; the AAIU-ref regex ignores them so the case_id stays stable.
- ⚠️ Legacy 2009 PDFs live under a *different* path
  `/sites/default/files/domain/Aviation/Veiligheid/Verslagen%20voorvallen/…`
  (literal spaces) — handled transparently by the generic absolutiser; case_id
  still derives from the bare filename + year heading.
- case_id: modern AAIU ref lowercased verbatim (`aaiu-2022-09-12-01` — no
  collision shape with Ireland's `YYYY-NNN`); legacy → `be-{year}-{slug}`,
  numeric collision suffix on conflict. All 164 case_ids unique on the live set.
- 38 rows lack a clean DD/MM/YYYY in the date cell (odd `<p>` wrapping or
  range text). date_of_occurrence is best-effort metadata; the PDF text carries
  the authoritative occurrence date downstream.
- DELAY 1.5 s between every HTTP request.

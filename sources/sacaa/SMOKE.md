# SACAA ingest — smoke results (2026-06-04, Mac)

```
.venv/bin/python -m sacaa_ingest.cli discover --db /tmp/sacaa-smoke.db
# discovered: 2722 (main 1010 + archive 1715, dups dropped)
#   Final 2649 / Preliminary 64 / Foreign 6 / Interim 2 / PASA 1
```

| case | era | tier | narrative |
|---|---|---|---|
| 6950 | 1998 archive | pdf | 1.8K ch |
| 9690 | 2018 main | pdf | 5.6K ch |
| zu-ppa-2023-01-02 | 2023 preliminary | pdf | 27K ch |

## Facts confirmed live

- TWO listing pages, entire dataset server-rendered as static tables
  (DataTables = client-side cosmetics; one GET per page).
- 4-col "latest" table (Title|Reg|Date|File) + 7-col main/archive tables
  (Year|Date|Type|Location|Name|Reg|File).
- Year column is a year OR a category (Preliminary/Interim/Foreign/PASA
  Reports); category rows carry the FULL date in the Date column, year
  rows have day+month only.
- case_id = numeric AIID id (tail of ref CA18/2/3/{id}) when Name is
  numeric; else {reg}-{date} slug for category rows.
- Blob hrefs contain spaces → percent-encode verbatim hrefs.
- ⚠️ Both big tables are EXACTLY 1000 rows — possible server-side cap in
  their WP plugin (archive t3 also lacks year 2004 entirely). Treat the
  DOM as the source of truth; weekly cycle keeps newest anyway.
- Some archive/prior-2010 PDFs are scans → tier 'scanned', skipped at build.

# KNKT ingest — smoke results (2026-06-04, Mac)

```
discover: 250 report-bearing rows of 858 (Final 178 / Preliminary 66 / Interim 6)
```

| case | tier | narrative | notes |
|---|---|---|---|
| KNKT.07.01.01.04 | pdf | 180K ch | Adam Air PK-KKW, space-filename era |
| KNKT.22.07.11.04 | pdf | 41K ch | ⚠️ year-trap VERIFIED: /2008/ 404 → /2022/ 200 |
| KNKT.26.04.02.04 | pdf | 42K ch | newest, dashed filename |

- Listing = ONE JSON call (row_count=20000, Referer required, 301-lowercase redirect).
- Folder year ≠ occurrence year for late-published reports → candidate_years
  tries occurrence year, then case-number year.
- Keterangan parse: {type}, {operator} ({aircraft}/{reg}); {location} / {case#}.
- case_id = KNKT.YY.MM.DD.NN canonical (old KNKT/07.01/… normalized), else reg+date.

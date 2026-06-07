# NSIA ingest — smoke results (2026-06-04, Mac)

| case | lang | tier | narrative |
|---|---|---|---|
| 2026-03 | English | pdf | 388K ch (Bristow LN-OIJ heli) |
| 2024-02 | Norwegian | pdf | 24K ch (nynorsk → P3 NO→EN) |

- Listing: /Aviation/Aviation/Published-reports?page=N — ⚠️ 1-INDEXED
  (?page=0 silently serves page 1); ~13 pages × 30 rows ≈ 390.
- ⚠️ BILINGUAL per-row (Lang. column): ~50/50 EN/NO; NO rows handled by
  the P3 NO→EN single-pass prompt (BFU/BEA precedent). lang stored.
- Metadata in listing (type/reg/date DD.MM.YYYY/location) + detail-page
  <td>Label</td><td>Value</td> table (Operator, Type of occurrence).
- PDF constructable: {detail}?pid=SHT-Report-ReportFile&attach=1.
- case_id 2024/02 → 2024-02.

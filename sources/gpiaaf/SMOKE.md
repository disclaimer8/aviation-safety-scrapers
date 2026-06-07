# GPIAAF Portugal ingest — smoke results (2026-06-07, Mac, headless)

```
discover (full walk, headless): 708 rows  (610 with report link -> 'new', 98 'no_report' bulletin-only)
  — scout estimated ~370; the real populated-year walk yields 708 (1946-2026)
fetch+build (sample 5, S3-capture flow):
```

| case_id | event_date | reg | narrative |
|---|---|---|---|
| 2026-aval-01 | 2026-02-28 | OE-FSB | 14,322 |
| 2024-accid-04 | 2024-08-30 | EC-LBV | **307,111** |
| 2024-aval-02 | 2024-08-10 | D-FOGO | 58,098 |
| 2024-aval-03 | 2024-07-27 | CS-TPR | 26,282 |
| 2024-accid-03 | 2024-07-03 | CS-XBC | 46,107 |

- **Headless works** — no anti-bot, no fingerprint block. `--headed`/GPIAAF_HEADED escape hatch kept.
- **S3 capture verified**: `?v=` SPA route → presigned S3 (60s expiry) captured from network events → immediate context.request.get. All 5 succeeded.
- PDFs bilingual PT‖EN line-by-line, clean text layers.
- Case_id shapes: `NN/ACCID/YYYY` → `2024-accid-03`-style (year-first normalize), NEW `AVAL` kind seen (avaliação) → `2026-aval-01`; rows without a processo id get `gpiaaf-{8hex}` fallback (frequent on recent incident rows).
- Cookie banner avoided (URL-navigation only); SPA-router homepage fallback detect+retry in place.
- 45 offline tests green (browser mocked).

DB: `gpiaaf_reports` (case_id PK) + `gpiaaf_accidents` (country DEFAULT 'PT', lang 'pt').

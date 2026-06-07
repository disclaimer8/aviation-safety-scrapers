# TAIC ingest — smoke results (2026-06-04, Mac)

## Live smoke

```
.venv/bin/python -m taic_ingest.cli discover --db /tmp/taic-smoke.db --max-pages 10
# discovered: 16 (AO-* only; listing is ALL modes — server ignores ?mode filter)
.venv/bin/python -m taic_ingest.cli fetch --db /tmp/taic-smoke.db --pdf-dir /tmp/taic-pdfs
.venv/bin/python -m taic_ingest.cli build --db /tmp/taic-smoke.db
```

| case | tier | narrative | notes |
|---|---|---|---|
| AO-2025-003 | html | 29,081 ch | ZK-IGD, full Details metadata |
| AO-2018-006 | html | 72,233 ch | ZK-HTB, 10 rich-content sections |
| AO-1995-009 | scanned | 874 ch | PDF = photocopier scan (no text layer); HTML executive summary kept |

## Facts confirmed live

- Listing: `/inquiries-recommendations?page=N` (0-indexed, 12 cards/page,
  ~98 pages all-modes); past-end pages return 200 + 0 cards → stop-on-empty.
- `?mode[0]=aviation` is **ignored server-side** (JS/AJAX filter) → walk all,
  filter `AO-` prefix client-side.
- Pills: `In progress` | `Published`. In-progress pages have no report —
  fetch only Published; discover resets a row to `new` when its pill flips.
- Modern (~2009+) inquiry pages: narrative in `field--name-field-rich-content`
  blocks (balanced-div extraction), metadata in `field--name-field-*` divs.
- Pre-~2000 PDFs are SCANS (ApeosPort copier, CCITT images) → pdftotext
  empty → `source_tier='scanned'`, narrative = HTML executive summary (real
  prose, kept when >= 300 chars).
- ⚠️ feasibility said old PDFs were text-layer — WRONG (ao-1995-009 is a scan).

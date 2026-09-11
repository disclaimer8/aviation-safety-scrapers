# NBAAI (Ukraine) Phase-1 smoke

Source: https://nbaai.gov.ua (НБРТ). Discovery via enquiry-sitemap.xml (~77
detail pages). MOST reports carry a clean Unicode Ukrainian HTML narrative body
(tier 'html'); a minority attach a final-report PDF that is EITHER clean Unicode
(tier 'pdf') OR scanned-image / non-Unicode font (tier 'ocr', ukr+rus).

## ⚠️ OCR runs ONLY on the mini-PC (tesseract+ocrmypdf+ukr+rus). Mac dev has no OCR.

```bash
# bounded discover + fetch + parse + build on a smoke db (run on mini-PC for OCR)
.venv/bin/python -m nbaai_ingest.cli discover --db smoke.db
.venv/bin/python -m nbaai_ingest.cli fetch    --db smoke.db --pdf-dir smoke-pdfs
.venv/bin/python -m nbaai_ingest.cli parse    --db smoke.db
.venv/bin/python -m nbaai_ingest.cli build    --db smoke.db

python -c "
import sqlite3
c=sqlite3.connect('smoke.db')
print('reports:', c.execute('SELECT COUNT(*) FROM nbaai_reports').fetchone()[0])
print('accidents:', c.execute('SELECT COUNT(*) FROM nbaai_accidents').fetchone()[0])
print('tiers:', dict(c.execute('SELECT source_tier,COUNT(*) FROM nbaai_reports GROUP BY source_tier')))
"
rm -f smoke.db smoke.db-wal smoke.db-shm && rm -rf smoke-pdfs
```

## parse() tiering
- pdftotext -> is_usable_text() (Cyrillic/Latin/English aviation markers, >=2)
  - usable & >= 600 chars -> tier 'pdf'
  - else (mojibake non-Unicode font OR scanned/empty) -> ocr_extract(pdf, "ukr+rus")
    - OCR usable & >= 80 chars -> tier 'ocr'
    - else fall back to HTML body (tier 'html') / 'short' / 'none'
- no PDF -> HTML narrative body -> tier 'html'

case_id is INTRINSIC: NBAAI-<REG>-<YYYY-MM-DD> (registration from the Latin URL
slug, robust to Cyrillic titles; event date DD.MM.YYYY from the body).

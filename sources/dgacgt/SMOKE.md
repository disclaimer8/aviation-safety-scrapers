# dgacgt-ingest — live smoke (2026-06-07)

Source: DGAC Guatemala / UIA — open autoindex under
https://www.dgac.gob.gt/wp-content/uploads/ORGANIZACION/UIA/INVESTIGACION DE ACCIDENTES/INFORMES FINALES/{year}/

## discover
- 200 PDFs found across year dirs 1999–2025 (2000, 2004, 2026 empty).
- 199/200 got a registration+ISO-date case_id (e.g. TG-MIC-2024-07-31).
- 1 no-date (filename year typo "20112"); 2 non-standard case_ids
  (military "FAG A-37B 1654" no civil reg; the typo). 0 case_id collisions.

## fetch + build (3 reports end-to-end)
| case_id            | tier  | narrative chars | report_no     | result  |
|--------------------|-------|-----------------|---------------|---------|
| TG-MIC-2024-07-31  | pdf   | 52,580          | UIA-A-11-2024 | built   |
| TG-GOL-2015-01-17  | pdf   | 59,538          | A-02-2015     | built   |
| TG-LOK-2008-01-19  | none  | 0 (scanned)     | —             | skipped |

- Spanish narrative retained verbatim (EN translation downstream).
- country=GT, site_slug = lowercased case_id (e.g. tg-mic-2024-07-31).
- 2008-era PDF is a genuine image scan (pdftotext = 9 chars) → scanned-tier
  gate correctly skips it.

## tests
- 49 passed (pytest), offline parse tests over live-captured fixtures.

## timer
- Requested Sun 21:30 UTC collides with griaa-cycle.timer → using Sun 21:45 UTC.

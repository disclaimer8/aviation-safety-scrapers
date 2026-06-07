# JST ingest — smoke results (2026-06-04, Mac)

25th source. JST Argentina (Junta de Seguridad en el Transporte).

```
discover --max-pages 40 : 593 doc-bearing rows
  (IB 254 / ISO 227 / INC 46 / IP 38 / IPROV 26 / LV 1 / INT 1)
events API: modo=2 (AVIATION), cantidad=2390, paginas.max=120, 20/page
manifest:   Index.json = 1939 keys, dict[zero-padded-expediente] -> [{tipo,path}]
```

| case_id | tipo | narrative | matricula | aircraft | notes |
|---|---|---|---|---|---|
| 120841344 | ISO | 14,547 ch | LV-HPW | CESSNA C-172 | ✅ ISO final >5K, clean ES text layer |
| 109333376 | IB  | 1,289 ch  | LV-MIF | PIPER PA-34 | shorter IB boletín (still ≥300 floor) |

- Join (verified): API `nro_expediente` `41546464/26` → strip `/YY` → digits → zfill(8)
  = manifest key `41546464`. ~47% of recent events carry a manifest doc;
  recent pages skew IP (preliminary), ISO finals live on older expedientes.
- Doc preference ISO > IB > INC > IPROV > IP — newest 3 pages were all IP/INC,
  so smoke discovered 40 pages to reach ISO finals.
- Build floor = 300 chars; country AR; source_url = the PDF URL; report_type = tipo.
- DELAY 1.5s. Plain httpx + browser UA (403 without UA), no Cloudflare.

## Reproduce
```
.venv/bin/python -m jst_ingest.cli discover --db /tmp/jst-smoke.db --max-pages 40
# keep 1 ISO + 1 IB row, sqlite UPDATE status='skipped' on the rest
.venv/bin/python -m jst_ingest.cli fetch --db /tmp/jst-smoke.db --pdf-dir /tmp/jst-smoke-pdfs
.venv/bin/python -m jst_ingest.cli build --db /tmp/jst-smoke.db
```

# AAIB India ingest — smoke results (2026-06-04, Mac)

```
.venv/bin/python -m india_ingest.cli discover --db /tmp/india-smoke.db
# discovered: 212 (248 Reports/*.pdf links minus 26 preliminary/interim
#              minus dups; 181 with VT-### registration from the filename)
.venv/bin/python -m india_ingest.cli fetch --db /tmp/india-smoke.db --pdf-dir /tmp/india-pdfs
.venv/bin/python -m india_ingest.cli build --db /tmp/india-smoke.db
```

| case | tier | narrative | extracted |
|---|---|---|---|
| 2022_VT-AMU | pdf | 50K ch | reg+date+aircraft+location |
| 2022_VT-PWI | pdf | ~45K ch | reg+date+operator(Pawan Hans)+S-76D+Mumbai Off-Shore |
| 2012_VT-PHH | pdf | 50K ch | "Accident to <Operator> <Type>" era format |

## Facts confirmed live

- ONE index page (no pagination/JS/anti-bot); bare relative hrefs
  `Reports/{YEAR}/{TYPE}/{file}.pdf`; filenames messy (spaces, mixed case).
- Dir names inconsistent: Accident/accident, Serious Incident/SeriousIncident,
  INCIDENT → normalized in parse_index.
- Skip rule: filename contains prelim/interim. Everything else
  (Final/Accepted/unmarked) = published report.
- PDFs text-layer both eras (2012 = 37-64K chars, 2025 = 123K).
- No official case numbering → synthetic case_id {year}_{VT-REG} with _2
  collision suffix; URL is the PK.
- Metadata only in the PDF; 3 era formats handled best-effort:
  new title-phrase ("involving Spice Jet's B-737-800 … on 01 May 2022"),
  mid ("Accident to Pawan Hans Helicopters Limited (PHHL) Bell 407 … at X"),
  old labeled table ("Aircraft Type : PC-12/45", "Registration : VT - DAR").
- ⚠️ registration regex: no trailing \b ('_' is a word char — VT_RGF_Sultanpur).

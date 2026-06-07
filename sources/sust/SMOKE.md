# SUST / STSB ingest — smoke results (2026-06-04, Mac)

```
discover: 3016 rows (ONE GET on the skeleton; data-lazyload URLs parsed)
fetch --max-rows 3 (3 newest): all 3 → parsed/built
fetch 3811 (multi-doc, Schlussbericht): parsed/built
```

| case_id (uid) | tier | report_type | lang | narrative | notes |
|---|---|---|---|---|---|
| 3844 | pdf | Rapporto preliminare | it | 1,228 ch | newest; _VB_I → it; HB-ZEJ AS 350 B3 |
| 3843 | pdf | Notification | de | 1,728 ch | HB-JHK A330; foreign place (no canton) |
| 3840 | pdf | Notification | de | 1,101 ch | UAV-MR28 (UAV- matriculation) |
| **3811** | **pdf** | **Schlussbericht** | **de** | **13,340 ch** | ✅ >5K Schlussbericht; D-EXIK EA400; _FB_D → de; chose FB over the VB_D + VB_e siblings |

Verified Schlussbericht 3811 narrative head:
`Schweizerische Sicherheitsuntersuchungsstelle SUST … Faktenbericht …` (text-layer, no OCR).

Pipeline facts confirmed live:
- ⚠️ www required (bare sust.admin.ch FAILS DNS).
- Step 1: ONE GET on the listAvExamination skeleton → 3,016 `<tr data-append-loaded data-lazyload>` rows; cHashes baked there, re-fetched each run, NEVER forged.
- Step 2: getEntry JSON shape exactly as scouted — `{uid, date "DD. MM. YYYY", place, canton (2-letter / foreign text / ABSENT), aircrafts:[{matriculation, manufacturer, type, category, …}], documents:[{name (LOCALIZED de/fr/it/en), url, extension, size, releasedate}]}`.
- Doc preference Schlussbericht>Summarischer>Faktenbericht>Vorbericht>Notification, keyed on filename code (_FB_/_VB_) then localized name.
- Lang from filename suffix _D/_F/_I/_E case-insensitively (3811 had lowercase `_e` Notification sibling); numeric old names → 'de'.
- case_id = str(uid) numeric; country 'CH'; source_url = documents[].url verbatim (absolute-ified).
- Doc-less rows (e.g. uid 3751) keep metadata but stay 'new' for weekly self-heal.
- DELAY 1.75s (admin.ch gov — polite).

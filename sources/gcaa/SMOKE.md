# GCAA ingest — smoke results (2026-06-04, Mac)

27th source, first Middle East. GCAA UAE (General Civil Aviation Authority,
Air Accident Investigation Sector).

```
discover (full) : 153 attachment-bearing rows  (171 items − 18 skipped)
API:  ONE SharePoint REST GET .../getbytitle('Incidents Investigation Reports')
      /items?$expand=AttachmentFiles&$top=500  → HTTP 200, 171 items, 2008-2024
shape: OData VERBOSE  {"d": {"results": [...]}}  (Accept: application/json;odata=verbose)
fields: SharePoint internal names use _x0020_ for spaces
        Reference_x0020_No, Registration_x0020_No, Aircraft_x0020_Type,
        Occurrence_x0020_Date, Occurrence_x0020_Category, Report_x0020_Status
Report_Status mix: Final 102 / Preliminary 34 / Summary 29 / Interim 4 / SafetyResearch 2
Category mix:      Accident 82 / Serious Incident 48 / Incident 41
```

| case_id | report_type | narrative | registration | aircraft | notes |
|---|---|---|---|---|---|
| aifn-0001-2013 | Final | 41,259 ch | A6-FTI | Bell Helicopter-Textron 206-3B | clean EN text layer, helicopter accident |
| aifn-0001-2014 | Final | 15,290 ch | A6-EID | Airbus A319 | clean EN text layer, summary report |

- case_id: `Reference_No` 'AIFN/0007/2013' → lowercase + non-alnum→'-' = `aifn-0007-2013`.
  Fallback `gcaa-{Id}` when Reference_No is null.
- Attachment URL: `ServerRelativeUrl` percent-encoded (filenames carry spaces/commas)
  + absolute-ified against https://www.gcaa.gov.ae. Verified live download + pdftotext.
- Stub skip: items with empty AttachmentFiles (e.g. Id 136 AIFN/0007/2021) are skipped.
- Build floor = 300 chars; country AE; source_url = the PDF URL; report_type = Report_Status.
- DELAY 1.5s. Plain httpx + browser UA + Accept: application/json;odata=verbose. No anti-bot.

## Deviations from the brief (live data 2026-06-04)
- Live response had **0 null-registration** items (brief expected ~30) and
  **0 multi-attachment** items (brief expected one). The fixture SYNTHESIZES a
  null-reg item (Id 900), a null-reference item (Id 901, gcaa-{Id} fallback),
  and a 2-attachment item (Id 902) so those code paths stay covered; the other
  5 items are real (incl. one foreign reg UP-A3003 and the real Id 136 stub).
- Multi-attachment policy: prefer a 'Final'-named attachment, else the last one.

## Reproduce
```
.venv/bin/python -m gcaa_ingest.cli discover --db /tmp/gcaa-smoke.db
# keep 2 Final rows, sqlite UPDATE status='skipped' on the rest
.venv/bin/python -m gcaa_ingest.cli fetch --db /tmp/gcaa-smoke.db --pdf-dir /tmp/gcaa-smoke-pdfs
.venv/bin/python -m gcaa_ingest.cli build --db /tmp/gcaa-smoke.db
```

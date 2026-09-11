# BAAID (Bahamas AAID) ingest — smoke notes

Source: https://www.baaid.org/accidents  (Wix site, ENGLISH, text-layer PDFs)
Source key: baaid   Country: BS   Tables: baaid_reports / baaid_accidents

## Enumeration (how the Wix DB is read)
The /accidents page is a single SERVER-RENDERED HTML page.  The full report
list is inline as <a href=".../_files/ugd/PREFIX_HASH.pdf"> anchors inside a
Wix rich-text "Aviation Occurrence Database" table, grouped under year headers
and three sections (under-investigation / Accidents & Serious Incidents /
Incidents & Short Investigations).  NO Wix data API (_api/cloud-data) call is
needed — everything is in the HTML.  We walk the document in order, MERGE
consecutive anchors sharing the same PDF (Wix splits a registration across
spans, e.g. "N702"+"SV" => "N702SV"), and emit one row per UNIQUE PDF file-id.
Live discover = 222 reports (2001-2026) — the full DB, not page 1.

## case_id
Intrinsic & stable = normalised Wix file-id "PREFIX-HASH".  Most listing rows
expose only registration + PDF (no OCC number); the OCC number lives in the PDF
text and is heterogeneous (OCC-YYYY/NNNN, OCC YYYY/NNNN, AO-YY-NNNNNN, typos),
so it is NOT the primary key — it is parsed at parse-time into report_type.
NO encounter-order suffixes.  site_slug = lowercased case_id.

## Smoke evidence
- discover: 222
- fetch+parse+build on 5 rows: 5/5/5, English narratives, country=BS,
  registrations + OCC/AO numbers + ISO dates extracted from PDF text.
- pytest: 41 passed (incl. non-bleed vs aaid/Kenya).

## Commands
python -m baaid_ingest.cli all --db baaid.db --pdf-dir pdfs

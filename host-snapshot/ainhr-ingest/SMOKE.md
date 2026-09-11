# AIN.HR (Croatia) ingest — smoke notes

Source key: `ainhr`. Country: HR. Croatian-language, text-layer PDFs.

Listing: https://ain.hr/kategorije/zrakoplovne-istrage/ (WordPress/Divi,
server-rendered, single page, no pagination, ~63 aviation posts).
Each post: https://ain.hr/istrage/<slug>/ links report PDFs under
ain.hr/wp-content/uploads/.

## case_id
The post slug IS the case_id (intrinsic, order-independent;
aircraft-location-date shape). No clean case number on the public site.
Normalised by `_normalize_case_id` (lower, [^a-z0-9]+ -> '-').
`site_slug` == normalised case_id.

## PDF preference (narrative pdf_url)
HR final ("Završno izvješće" / *zavrsno_izvjesce*)
  > HR preliminary ("Preliminarno izvješće" / *preliminarno*)
  > any other HR pdf.
EN translations ("Final report"/"Preliminary report"/*final_report*) kept in
pdf_url_en. narrative_text stays Croatian (HR->EN downstream in P3).

## tiers / skip
- pdf:     >= 600 chars
- short:   500..600
- scanned: 1..499 chars (image-only scan) -> build() SKIPS
- none:    0 chars (no/empty PDF)         -> build() SKIPS

## run
    python -m ainhr_ingest.cli all --db ainhr.db --pdf-dir pdfs

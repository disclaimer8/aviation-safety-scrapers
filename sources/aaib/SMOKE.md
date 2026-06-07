# Phase-1 smoke (5 reports)

Date: 2026-06-01
Reports: 5 newest AAIB investigations.

Observed (all 5 built):
- source_tier = pdf for all 5 (Tier-2 pdftotext extraction succeeded; no body fallbacks).
- narrative_text lengths: AW139 G-CIMU 83067 (field-investigation), G-TALX 12433,
  G-CKLY 13418, G-MOOD 9584, G-ZIRA 2298 (correspondence) — all well over the 600 gate.
- site_slug generated as crash-<aircraft>-<reg>-<location>, e.g.
  crash-leonardo-aw139-g-cimu-norwich-airport.
- narrative is genuine AAIB report prose with structured field labels (Aircraft Type,
  Engines, Date & Time, Location, Injuries, Nature of Damage, ...).

Conclusion: discover -> fetch -> parse(PDF) -> build works end-to-end on live GOV.UK
data; Tier-2 extraction confirmed on real AAIB PDFs. Ready for mini-PC deploy (Task 9).

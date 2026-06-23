-- 006_wayback_extract.sql
-- Stage-2 extraction state machine on staged_wayback_documents: OCR text artifact,
-- per-document status, retry accounting, and the promoted event link.

ALTER TABLE staged_wayback_documents ADD COLUMN extraction_status TEXT NOT NULL
  DEFAULT 'pending' CHECK(extraction_status IN (
    'pending',
    'ocr_done',
    'extracted',
    'failed',
    'skipped'
  ));

ALTER TABLE staged_wayback_documents ADD COLUMN ocr_text_path TEXT;

ALTER TABLE staged_wayback_documents ADD COLUMN extraction_error TEXT;

ALTER TABLE staged_wayback_documents ADD COLUMN extraction_attempts INTEGER NOT NULL
  DEFAULT 0;

ALTER TABLE staged_wayback_documents ADD COLUMN event_id INTEGER
  REFERENCES events(id);

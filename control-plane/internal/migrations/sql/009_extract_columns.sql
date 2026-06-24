-- 009_extract_columns.sql
-- Give staged_regional_documents and staged_foreign_documents the same
-- download + extraction state machine columns staged_wayback_documents has
-- (005/006), so the unified extract worker (Worker 4) can process them.
ALTER TABLE staged_regional_documents ADD COLUMN download_status TEXT NOT NULL DEFAULT 'pending'
  CHECK(download_status IN ('pending','downloaded','failed','skipped'));
ALTER TABLE staged_regional_documents ADD COLUMN local_file_path TEXT;
ALTER TABLE staged_regional_documents ADD COLUMN digest TEXT;
ALTER TABLE staged_regional_documents ADD COLUMN ocr_text_path TEXT;
ALTER TABLE staged_regional_documents ADD COLUMN extraction_status TEXT NOT NULL DEFAULT 'pending'
  CHECK(extraction_status IN ('pending','ocr_done','extracted','failed','skipped'));
ALTER TABLE staged_regional_documents ADD COLUMN extraction_error TEXT;
ALTER TABLE staged_regional_documents ADD COLUMN extraction_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE staged_regional_documents ADD COLUMN event_id INTEGER REFERENCES events(id);

ALTER TABLE staged_foreign_documents ADD COLUMN download_status TEXT NOT NULL DEFAULT 'pending'
  CHECK(download_status IN ('pending','downloaded','failed','skipped'));
ALTER TABLE staged_foreign_documents ADD COLUMN local_file_path TEXT;
ALTER TABLE staged_foreign_documents ADD COLUMN digest TEXT;
ALTER TABLE staged_foreign_documents ADD COLUMN ocr_text_path TEXT;
ALTER TABLE staged_foreign_documents ADD COLUMN extraction_status TEXT NOT NULL DEFAULT 'pending'
  CHECK(extraction_status IN ('pending','ocr_done','extracted','failed','skipped'));
ALTER TABLE staged_foreign_documents ADD COLUMN extraction_error TEXT;
ALTER TABLE staged_foreign_documents ADD COLUMN extraction_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE staged_foreign_documents ADD COLUMN event_id INTEGER REFERENCES events(id);

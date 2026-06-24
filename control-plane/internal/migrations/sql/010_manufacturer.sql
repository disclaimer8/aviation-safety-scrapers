-- 009_manufacturer.sql
-- Staging table for the manufacturer documents worker (Airbus Safety First discovery):
-- capture publication references, URLs, and content extraction/download state.
CREATE TABLE staged_manufacturer_documents (
  id INTEGER PRIMARY KEY,
  manufacturer TEXT NOT NULL,
  publication TEXT NOT NULL,
  issue_ref TEXT NOT NULL,
  title TEXT NOT NULL,
  publication_date TEXT,
  original_url TEXT NOT NULL,
  report_url TEXT,
  mimetype TEXT,
  download_status TEXT NOT NULL DEFAULT 'pending' CHECK(download_status IN ('pending','downloaded','failed','skipped')),
  local_file_path TEXT,
  digest TEXT,
  ocr_text_path TEXT,
  extraction_status TEXT NOT NULL DEFAULT 'pending' CHECK(extraction_status IN ('pending','ocr_done','extracted','failed','skipped')),
  extraction_error TEXT,
  extraction_attempts INTEGER NOT NULL DEFAULT 0,
  event_id INTEGER REFERENCES events(id),
  created_at INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec') * 1000 AS INTEGER)),
  UNIQUE(publication, issue_ref)
) STRICT;

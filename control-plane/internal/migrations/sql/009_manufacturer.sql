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
  download_status TEXT,
  extraction_status TEXT,
  event_id INTEGER,
  created_at INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec') * 1000 AS INTEGER)),
  UNIQUE(publication, issue_ref)
) STRICT;

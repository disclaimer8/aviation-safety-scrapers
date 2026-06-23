-- 005_wayback.sql
-- Adds the Wayback acquisition worker's target column and staging table.

ALTER TABLE countries
  ADD COLUMN wayback_target TEXT;

CREATE TABLE staged_wayback_documents (
  id INTEGER PRIMARY KEY,
  crawl_job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
  country_id INTEGER NOT NULL REFERENCES countries(id),
  original_url TEXT NOT NULL,
  archived_url TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  mimetype TEXT NOT NULL,
  digest TEXT NOT NULL,
  length INTEGER,
  local_file_path TEXT,
  checksum TEXT,
  download_status TEXT NOT NULL DEFAULT 'pending' CHECK(download_status IN (
    'pending',
    'downloaded',
    'failed',
    'skipped'
  )),
  created_at INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec') * 1000 AS INTEGER)),
  UNIQUE(country_id, digest)
) STRICT;

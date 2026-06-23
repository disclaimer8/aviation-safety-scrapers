-- 007_regional.sql
-- Staging table for the regional worker: accident records discovered at a
-- regional investigation body (ECCAA/BAGAIA/IAC) for a member country.
CREATE TABLE staged_regional_documents (
  id INTEGER PRIMARY KEY,
  crawl_job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
  country_id INTEGER NOT NULL REFERENCES countries(id),
  body_code TEXT NOT NULL CHECK(body_code IN ('ECCAA', 'BAGAIA', 'IAC')),
  ref TEXT NOT NULL,
  title TEXT NOT NULL,
  occurrence_date TEXT,
  original_url TEXT NOT NULL,
  report_url TEXT,
  mimetype TEXT,
  created_at INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec') * 1000 AS INTEGER)),
  UNIQUE(body_code, ref)
) STRICT;

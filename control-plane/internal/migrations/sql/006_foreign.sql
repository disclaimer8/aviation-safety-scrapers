-- 006_foreign.sql
-- Staging table for the foreign-search worker: accident records discovered at a
-- foreign accredited-representative authority (NTSB/BEA/ATSB) for a delegated
-- country.
CREATE TABLE staged_foreign_documents (
  id INTEGER PRIMARY KEY,
  crawl_job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
  country_id INTEGER NOT NULL REFERENCES countries(id),
  authority TEXT NOT NULL CHECK(authority IN ('ntsb', 'bea', 'atsb')),
  foreign_ref TEXT NOT NULL,
  title TEXT NOT NULL,
  occurrence_date TEXT,
  original_url TEXT NOT NULL,
  report_url TEXT,
  mimetype TEXT,
  created_at INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec') * 1000 AS INTEGER)),
  UNIQUE(authority, foreign_ref)
) STRICT;

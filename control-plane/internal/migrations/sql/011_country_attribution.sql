-- 011_country_attribution.sql
-- GO-CP-1 fix: the regional bodies (ECCAA/BAGAIA/IAC) and the BEA foreign-search
-- listing are body-wide — a single Search() call returns every accident the body
-- has ever published, not just the ones for the crawl job's country. Staging
-- every record under whichever country's job happened to run first silently
-- misattributed accidents (confirmed live: a Belarus accident and a Kazakh Mi-8
-- were both stamped RU via IAC). country_id must become nullable so these
-- body-wide sources can stage a record WITHOUT an unearned country claim; the
-- extract step resolves the true occurrence country from the report content
-- (or, when the listing carries a deterministic per-record country, from that)
-- and writes THAT into events.occurrence_country_id instead.
--
-- SQLite has no ALTER COLUMN, so the NOT NULL constraint is dropped by
-- recreating each table and copying the data across (11 explicit columns each,
-- to stay independent of on-disk column order). Neither table is an FK target
-- for any other table, so no other constraint is affected by the recreate.

CREATE TABLE staged_regional_documents_new (
  id INTEGER PRIMARY KEY,
  crawl_job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
  country_id INTEGER REFERENCES countries(id),
  body_code TEXT NOT NULL CHECK(body_code IN ('ECCAA', 'BAGAIA', 'IAC')),
  ref TEXT NOT NULL,
  title TEXT NOT NULL,
  occurrence_date TEXT,
  original_url TEXT NOT NULL,
  report_url TEXT,
  mimetype TEXT,
  created_at INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec') * 1000 AS INTEGER)),
  download_status TEXT NOT NULL DEFAULT 'pending' CHECK(download_status IN ('pending','downloaded','failed','skipped')),
  local_file_path TEXT,
  digest TEXT,
  ocr_text_path TEXT,
  extraction_status TEXT NOT NULL DEFAULT 'pending' CHECK(extraction_status IN ('pending','ocr_done','extracted','failed','skipped')),
  extraction_error TEXT,
  extraction_attempts INTEGER NOT NULL DEFAULT 0,
  event_id INTEGER REFERENCES events(id),
  UNIQUE(body_code, ref)
) STRICT;

INSERT INTO staged_regional_documents_new
  (id, crawl_job_id, country_id, body_code, ref, title, occurrence_date, original_url,
   report_url, mimetype, created_at, download_status, local_file_path, digest,
   ocr_text_path, extraction_status, extraction_error, extraction_attempts, event_id)
SELECT
  id, crawl_job_id, country_id, body_code, ref, title, occurrence_date, original_url,
  report_url, mimetype, created_at, download_status, local_file_path, digest,
  ocr_text_path, extraction_status, extraction_error, extraction_attempts, event_id
FROM staged_regional_documents;

DROP TABLE staged_regional_documents;
ALTER TABLE staged_regional_documents_new RENAME TO staged_regional_documents;

CREATE TABLE staged_foreign_documents_new (
  id INTEGER PRIMARY KEY,
  crawl_job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
  country_id INTEGER REFERENCES countries(id),
  authority TEXT NOT NULL CHECK(authority IN ('ntsb', 'bea', 'atsb')),
  foreign_ref TEXT NOT NULL,
  title TEXT NOT NULL,
  occurrence_date TEXT,
  original_url TEXT NOT NULL,
  report_url TEXT,
  mimetype TEXT,
  created_at INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec') * 1000 AS INTEGER)),
  download_status TEXT NOT NULL DEFAULT 'pending' CHECK(download_status IN ('pending','downloaded','failed','skipped')),
  local_file_path TEXT,
  digest TEXT,
  ocr_text_path TEXT,
  extraction_status TEXT NOT NULL DEFAULT 'pending' CHECK(extraction_status IN ('pending','ocr_done','extracted','failed','skipped')),
  extraction_error TEXT,
  extraction_attempts INTEGER NOT NULL DEFAULT 0,
  event_id INTEGER REFERENCES events(id),
  UNIQUE(authority, foreign_ref)
) STRICT;

INSERT INTO staged_foreign_documents_new
  (id, crawl_job_id, country_id, authority, foreign_ref, title, occurrence_date, original_url,
   report_url, mimetype, created_at, download_status, local_file_path, digest,
   ocr_text_path, extraction_status, extraction_error, extraction_attempts, event_id)
SELECT
  id, crawl_job_id, country_id, authority, foreign_ref, title, occurrence_date, original_url,
  report_url, mimetype, created_at, download_status, local_file_path, digest,
  ocr_text_path, extraction_status, extraction_error, extraction_attempts, event_id
FROM staged_foreign_documents;

DROP TABLE staged_foreign_documents;
ALTER TABLE staged_foreign_documents_new RENAME TO staged_foreign_documents;

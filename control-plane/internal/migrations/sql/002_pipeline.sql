CREATE TABLE events (
  id INTEGER PRIMARY KEY,
  date TEXT,
  date_precision TEXT NOT NULL DEFAULT 'unknown' CHECK(date_precision IN (
    'exact',
    'month',
    'year',
    'unknown'
  )),
  occurrence_country_id INTEGER REFERENCES countries(id),
  location TEXT,
  latitude REAL CHECK(latitude IS NULL OR latitude BETWEEN -90 AND 90),
  longitude REAL CHECK(longitude IS NULL OR longitude BETWEEN -180 AND 180),
  aircraft_registration TEXT,
  aircraft_type TEXT,
  manufacturer TEXT,
  operator_name TEXT,
  flight_number TEXT,
  fatalities INTEGER CHECK(fatalities IS NULL OR fatalities >= 0),
  injuries INTEGER CHECK(injuries IS NULL OR injuries >= 0),
  event_type TEXT NOT NULL DEFAULT 'unknown' CHECK(event_type IN (
    'accident',
    'serious_incident',
    'incident',
    'hijacking',
    'unknown'
  )),
  investigation_status TEXT NOT NULL DEFAULT 'unknown' CHECK(investigation_status IN (
    'final_report_available',
    'preliminary_report_available',
    'investigation_open',
    'no_report_found',
    'unknown'
  )),
  confidence_score INTEGER NOT NULL CHECK(confidence_score BETWEEN 0 AND 100),
  dedup_status TEXT NOT NULL DEFAULT 'unreviewed' CHECK(dedup_status IN (
    'unreviewed',
    'auto_merged',
    'soft_linked',
    'manual_review',
    'distinct'
  )),
  needs_official_confirmation INTEGER NOT NULL DEFAULT 0
    CHECK(needs_official_confirmation IN (0, 1)),
  created_at INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec') * 1000 AS INTEGER)),
  updated_at INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec') * 1000 AS INTEGER))
) STRICT;

CREATE TABLE reports (
  id INTEGER PRIMARY KEY,
  event_id INTEGER NOT NULL REFERENCES events(id),
  source_id INTEGER NOT NULL REFERENCES sources(id),
  report_type TEXT NOT NULL CHECK(report_type IN (
    'final',
    'preliminary',
    'interim',
    'factual',
    'safety_recommendation',
    'press_release',
    'index_record',
    'media_article'
  )),
  title TEXT NOT NULL,
  language TEXT NOT NULL,
  original_url TEXT NOT NULL,
  archived_url TEXT,
  pdf_url TEXT,
  published_date TEXT,
  accessed_at INTEGER NOT NULL,
  checksum TEXT,
  local_file_path TEXT,
  source_tier INTEGER NOT NULL CHECK(source_tier BETWEEN 1 AND 6),
  extraction_status TEXT NOT NULL DEFAULT 'pending' CHECK(extraction_status IN (
    'pending',
    'extracted',
    'failed',
    'manual_review'
  )),
  copyright_status TEXT NOT NULL CHECK(copyright_status IN (
    'official_public',
    'metadata_only',
    'unknown',
    'do_not_store_fulltext'
  )),
  notes TEXT
) STRICT;

CREATE TABLE event_source_links (
  id INTEGER PRIMARY KEY,
  event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  source_id INTEGER NOT NULL REFERENCES sources(id),
  source_event_id TEXT,
  source_url TEXT NOT NULL,
  matched_internal_event_id INTEGER REFERENCES events(id),
  match_confidence INTEGER CHECK(
    match_confidence IS NULL OR match_confidence BETWEEN 0 AND 100
  ),
  match_reason TEXT,
  created_at INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec') * 1000 AS INTEGER)),
  UNIQUE(event_id, source_id, source_url)
) STRICT;

CREATE UNIQUE INDEX idx_event_source_native_id
  ON event_source_links(source_id, source_event_id)
  WHERE source_event_id IS NOT NULL;

CREATE TABLE investigation_participants (
  id INTEGER PRIMARY KEY,
  event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  country_id INTEGER NOT NULL REFERENCES countries(id),
  authority_id INTEGER REFERENCES authorities(id),
  role TEXT NOT NULL CHECK(role IN (
    'state_of_occurrence',
    'state_of_registry',
    'state_of_operator',
    'state_of_design',
    'state_of_manufacture',
    'state_of_engine_manufacture',
    'accredited_representative',
    'delegated_investigator',
    'assisting_authority'
  )),
  source_url TEXT NOT NULL,
  notes TEXT
) STRICT;

CREATE TABLE crawl_jobs (
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES sources(id),
  country_id INTEGER REFERENCES countries(id),
  job_type TEXT NOT NULL CHECK(job_type IN (
    'authority_health_check',
    'archive_crawl',
    'pdf_discovery',
    'icao_elibrary_search',
    'ntsb_foreign_search',
    'bea_foreign_search',
    'atsb_search',
    'wayback_cdx',
    'direct_request_needed'
  )),
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN (
    'pending',
    'running',
    'success',
    'failed',
    'partial',
    'manual_review'
  )),
  started_at INTEGER,
  finished_at INTEGER,
  error TEXT,
  stats_json TEXT,
  created_at INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec') * 1000 AS INTEGER))
) STRICT;

CREATE TABLE crawl_errors (
  id INTEGER PRIMARY KEY,
  crawl_job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
  url TEXT NOT NULL,
  error_type TEXT NOT NULL CHECK(error_type IN (
    'tls_error',
    'timeout',
    'dns_error',
    'nx_domain',
    'http_403',
    'http_404',
    'http_500',
    'parse_error',
    'robots_blocked',
    'unknown'
  )),
  message TEXT NOT NULL,
  created_at INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec') * 1000 AS INTEGER))
) STRICT;

CREATE INDEX idx_events_date_registration_country
  ON events(date, aircraft_registration, occurrence_country_id);
CREATE INDEX idx_events_fallback_match
  ON events(date, aircraft_type, operator_name, fatalities);
CREATE INDEX idx_events_occurrence_country ON events(occurrence_country_id);
CREATE INDEX idx_reports_event ON reports(event_id);
CREATE INDEX idx_reports_source ON reports(source_id);
CREATE INDEX idx_event_source_links_event ON event_source_links(event_id);
CREATE INDEX idx_event_source_links_source ON event_source_links(source_id);
CREATE INDEX idx_event_source_links_matched
  ON event_source_links(matched_internal_event_id);
CREATE INDEX idx_investigation_participants_event
  ON investigation_participants(event_id);
CREATE INDEX idx_investigation_participants_country
  ON investigation_participants(country_id);
CREATE INDEX idx_investigation_participants_authority
  ON investigation_participants(authority_id);
CREATE INDEX idx_crawl_jobs_status_type ON crawl_jobs(status, job_type);
CREATE INDEX idx_crawl_jobs_source ON crawl_jobs(source_id);
CREATE INDEX idx_crawl_jobs_country ON crawl_jobs(country_id);
CREATE INDEX idx_crawl_errors_job ON crawl_errors(crawl_job_id);

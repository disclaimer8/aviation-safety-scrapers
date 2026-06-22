CREATE TABLE import_runs (
  id INTEGER PRIMARY KEY,
  importer TEXT NOT NULL,
  source_url TEXT NOT NULL,
  source_snapshot_id INTEGER
    REFERENCES source_snapshots(id) ON DELETE RESTRICT,
  started_at INTEGER NOT NULL,
  finished_at INTEGER,
  status TEXT NOT NULL CHECK(status IN (
    'running',
    'success',
    'partial',
    'failed',
    'unchanged'
  )),
  parsed_count INTEGER NOT NULL DEFAULT 0 CHECK(parsed_count >= 0),
  applied_count INTEGER NOT NULL DEFAULT 0 CHECK(applied_count >= 0),
  warning_count INTEGER NOT NULL DEFAULT 0 CHECK(warning_count >= 0),
  conflict_count INTEGER NOT NULL DEFAULT 0 CHECK(conflict_count >= 0),
  error_summary TEXT
) STRICT;

CREATE TABLE source_snapshots (
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES sources(id),
  source_url TEXT NOT NULL,
  final_url TEXT,
  status_code INTEGER,
  content_type TEXT,
  etag TEXT,
  last_modified TEXT,
  fetched_at INTEGER NOT NULL,
  checksum TEXT NOT NULL,
  raw_body BLOB,
  artifact_path TEXT,
  size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0)
) STRICT;

CREATE UNIQUE INDEX idx_snapshots_source_checksum
  ON source_snapshots(source_id, checksum);

-- 17.5.C: source snapshots are immutable after insert. Any attempt to change the
-- identity (primary key) or content/provenance columns aborts. This protects
-- deterministic re-derivation: a recorded provenance reference must always point
-- at the exact bytes that were fetched.
CREATE TRIGGER source_snapshots_immutable
BEFORE UPDATE ON source_snapshots
WHEN
  OLD.id IS NOT NEW.id
  OR OLD.source_id IS NOT NEW.source_id
  OR OLD.source_url IS NOT NEW.source_url
  OR OLD.final_url IS NOT NEW.final_url
  OR OLD.status_code IS NOT NEW.status_code
  OR OLD.content_type IS NOT NEW.content_type
  OR OLD.etag IS NOT NEW.etag
  OR OLD.last_modified IS NOT NEW.last_modified
  OR OLD.fetched_at IS NOT NEW.fetched_at
  OR OLD.checksum IS NOT NEW.checksum
  OR OLD.raw_body IS NOT NEW.raw_body
  OR OLD.artifact_path IS NOT NEW.artifact_path
  OR OLD.size_bytes IS NOT NEW.size_bytes
BEGIN
  SELECT RAISE(ABORT, 'source_snapshots rows are immutable');
END;

CREATE TABLE staged_authorities (
  id INTEGER PRIMARY KEY,
  import_run_id INTEGER NOT NULL REFERENCES import_runs(id) ON DELETE CASCADE,
  country_label TEXT NOT NULL,
  resolved_country_id INTEGER REFERENCES countries(id),
  authority_name TEXT NOT NULL,
  raw_contact TEXT,
  website_url TEXT,
  archive_url TEXT,
  contact_email TEXT,
  contact_phone TEXT,
  icao_updated_date TEXT,
  warnings_json TEXT,
  record_checksum TEXT NOT NULL,
  UNIQUE(import_run_id, record_checksum)
) STRICT;

CREATE TABLE staged_regional_bodies (
  id INTEGER PRIMARY KEY,
  import_run_id INTEGER NOT NULL REFERENCES import_runs(id) ON DELETE CASCADE,
  code TEXT NOT NULL,
  description TEXT,
  region TEXT,
  website_url TEXT,
  body_class TEXT NOT NULL CHECK(body_class IN ('raio', 'icm', 'regional_body')),
  member_labels_json TEXT,
  observer_labels_json TEXT,
  warnings_json TEXT,
  record_checksum TEXT NOT NULL,
  UNIQUE(import_run_id, record_checksum)
) STRICT;

CREATE TABLE field_overrides (
  id INTEGER PRIMARY KEY,
  entity_type TEXT NOT NULL,
  entity_id INTEGER NOT NULL,
  field_name TEXT NOT NULL,
  value TEXT,
  value_type TEXT NOT NULL,
  reason TEXT NOT NULL,
  author TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
  created_at INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec') * 1000 AS INTEGER)),
  updated_at INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec') * 1000 AS INTEGER))
) STRICT;

CREATE UNIQUE INDEX idx_active_field_override
  ON field_overrides(entity_type, entity_id, field_name)
  WHERE active = 1;

-- 17.5.B: field-level authority provenance. Each row records the effective value
-- of one authority field and where that value came from. Exactly one current
-- record per (authority_id, field_name) is enforced by the primary key. Optional
-- snapshot_id / override_id let exports resolve provenance back to immutable
-- source_snapshots or curated field_overrides. effective.ApplyAuthority (Task 8)
-- and export (Task 12) build on this table.
CREATE TABLE authority_field_provenance (
  authority_id INTEGER NOT NULL REFERENCES authorities(id) ON DELETE CASCADE,
  field_name TEXT NOT NULL,
  effective_value TEXT,
  provenance_kind TEXT NOT NULL CHECK(provenance_kind IN (
    'seed',
    'icao_snapshot',
    'curated_override'
  )),
  snapshot_id INTEGER REFERENCES source_snapshots(id) ON DELETE RESTRICT,
  override_id INTEGER REFERENCES field_overrides(id) ON DELETE RESTRICT,
  updated_at INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec') * 1000 AS INTEGER)),
  PRIMARY KEY (authority_id, field_name),
  -- A snapshot-sourced field must cite a snapshot; an override-sourced field must
  -- cite an override; a seed cites neither. Keeps provenance self-consistent.
  CHECK(
    (provenance_kind = 'icao_snapshot' AND snapshot_id IS NOT NULL AND override_id IS NULL)
    OR (provenance_kind = 'curated_override' AND override_id IS NOT NULL AND snapshot_id IS NULL)
    OR (provenance_kind = 'seed' AND snapshot_id IS NULL AND override_id IS NULL)
  )
) STRICT;

CREATE INDEX idx_authority_field_provenance_snapshot
  ON authority_field_provenance(snapshot_id);
CREATE INDEX idx_authority_field_provenance_override
  ON authority_field_provenance(override_id);

CREATE TABLE import_conflicts (
  id INTEGER PRIMARY KEY,
  import_run_id INTEGER NOT NULL REFERENCES import_runs(id) ON DELETE CASCADE,
  staged_authority_id INTEGER REFERENCES staged_authorities(id) ON DELETE CASCADE,
  staged_regional_body_id INTEGER
    REFERENCES staged_regional_bodies(id) ON DELETE CASCADE,
  target_entity_type TEXT NOT NULL,
  target_entity_id INTEGER NOT NULL,
  field_name TEXT NOT NULL,
  current_value TEXT,
  incoming_value TEXT,
  override_value TEXT,
  reason TEXT NOT NULL,
  review_status TEXT NOT NULL DEFAULT 'open' CHECK(review_status IN (
    'open',
    'accepted_incoming',
    'kept_curated',
    'resolved_manually'
  )),
  resolution TEXT,
  created_at INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec') * 1000 AS INTEGER)),
  resolved_at INTEGER
) STRICT;

CREATE UNIQUE INDEX idx_import_conflicts_idempotent
  ON import_conflicts(
    import_run_id,
    COALESCE(staged_authority_id, 0),
    COALESCE(staged_regional_body_id, 0),
    target_entity_type,
    target_entity_id,
    field_name,
    COALESCE(incoming_value, '')
  );

CREATE INDEX idx_import_conflicts_open
  ON import_conflicts(review_status)
  WHERE review_status = 'open';

CREATE TABLE authority_requests (
  id INTEGER PRIMARY KEY,
  authority_id INTEGER NOT NULL REFERENCES authorities(id),
  status TEXT NOT NULL DEFAULT 'not_sent' CHECK(status IN (
    'not_sent',
    'sent',
    'replied',
    'bounced',
    'no_response'
  )),
  subject TEXT,
  body TEXT,
  sent_at INTEGER,
  replied_at INTEGER,
  response_notes TEXT,
  created_at INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec') * 1000 AS INTEGER)),
  updated_at INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec') * 1000 AS INTEGER))
) STRICT;

CREATE INDEX idx_import_runs_snapshot ON import_runs(source_snapshot_id);
CREATE INDEX idx_source_snapshots_source ON source_snapshots(source_id);
CREATE INDEX idx_staged_authorities_run ON staged_authorities(import_run_id);
CREATE INDEX idx_staged_authorities_country ON staged_authorities(resolved_country_id);
CREATE INDEX idx_staged_regional_bodies_run
  ON staged_regional_bodies(import_run_id);
CREATE INDEX idx_import_conflicts_run ON import_conflicts(import_run_id);
CREATE INDEX idx_import_conflicts_staged_authority
  ON import_conflicts(staged_authority_id);
CREATE INDEX idx_import_conflicts_staged_regional_body
  ON import_conflicts(staged_regional_body_id);
CREATE INDEX idx_authority_requests_authority ON authority_requests(authority_id);

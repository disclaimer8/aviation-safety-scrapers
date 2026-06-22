CREATE TABLE countries (
  id INTEGER PRIMARY KEY,
  iso2 TEXT NOT NULL UNIQUE CHECK(length(iso2) = 2),
  iso3 TEXT NOT NULL UNIQUE CHECK(length(iso3) = 3),
  name TEXT NOT NULL,
  region TEXT NOT NULL,
  policy_status TEXT NOT NULL CHECK(policy_status IN (
    'allowed',
    'indirect_public_only',
    'excluded'
  )),
  coverage_status TEXT NOT NULL CHECK(coverage_status IN (
    'direct_public_archive',
    'delegated_to_foreign_authority',
    'regional_raio',
    'official_contact_only',
    'source_exists_unstable',
    'no_public_archive',
    'policy_excluded',
    'unknown'
  )),
  coverage_score INTEGER NOT NULL CHECK(coverage_score BETWEEN 0 AND 5),
  effort_score INTEGER NOT NULL CHECK(effort_score BETWEEN 1 AND 5),
  expected_records INTEGER NOT NULL DEFAULT 0 CHECK(expected_records >= 0),
  expected_source_quality INTEGER NOT NULL DEFAULT 1
    CHECK(expected_source_quality BETWEEN 1 AND 5),
  priority_score REAL NOT NULL DEFAULT 0,
  country_group TEXT CHECK(
    country_group IS NULL OR country_group IN ('A', 'B', 'C1', 'C2', 'C3', 'D')
  ),
  refresh_cadence TEXT,
  last_checked_at INTEGER,
  notes TEXT
) STRICT;

CREATE TABLE authorities (
  id INTEGER PRIMARY KEY,
  country_id INTEGER NOT NULL REFERENCES countries(id),
  normalized_name TEXT NOT NULL,
  name TEXT NOT NULL,
  type TEXT NOT NULL CHECK(type IN (
    'national_aai',
    'caa',
    'ministry',
    'regional_raio',
    'foreign_aai',
    'manufacturer_state_aai',
    'operator_state_aai',
    'registry_state_aai'
  )),
  website_url TEXT,
  archive_url TEXT,
  contact_email TEXT,
  contact_phone TEXT,
  source_url TEXT NOT NULL,
  source_name TEXT NOT NULL,
  has_public_archive INTEGER CHECK(
    has_public_archive IS NULL OR has_public_archive IN (0, 1)
  ),
  status TEXT NOT NULL DEFAULT 'unknown' CHECK(status IN (
    'ok',
    'empty_archive',
    'tls_error',
    'nx_domain',
    'suspended',
    'forbidden',
    'changed_structure',
    'manual_review_needed',
    'unknown'
  )),
  -- Row-level snapshot reference is retained for backward compatibility, but
  -- field-level provenance (authority_field_provenance, see 003) is the
  -- authoritative source of per-field provenance per spec 17.5.B. The FK uses
  -- ON DELETE RESTRICT so a referenced snapshot cannot be deleted (17.5.C).
  source_snapshot_id INTEGER
    REFERENCES source_snapshots(id) ON DELETE RESTRICT,
  last_checked_at INTEGER,
  notes TEXT,
  UNIQUE(country_id, normalized_name, type)
) STRICT;

CREATE TABLE regional_bodies (
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  body_class TEXT NOT NULL CHECK(body_class IN ('raio', 'icm', 'regional_body')),
  website_url TEXT,
  source_url TEXT NOT NULL,
  notes TEXT
) STRICT;

CREATE TABLE regional_body_members (
  regional_body_id INTEGER NOT NULL
    REFERENCES regional_bodies(id) ON DELETE CASCADE,
  country_id INTEGER NOT NULL REFERENCES countries(id),
  role TEXT NOT NULL,
  source_url TEXT NOT NULL,
  PRIMARY KEY(regional_body_id, country_id, role)
) STRICT;

CREATE TABLE sources (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  url TEXT NOT NULL,
  canonical_url TEXT NOT NULL,
  source_type TEXT NOT NULL CHECK(source_type IN (
    'official_aai',
    'official_foreign_accredited_rep',
    'icao_elibrary',
    'regulator',
    'ministry',
    'operator',
    'manufacturer',
    'regional_body',
    'trusted_index',
    'media',
    'wayback'
  )),
  source_tier INTEGER NOT NULL CHECK(source_tier BETWEEN 1 AND 6),
  robots_policy TEXT,
  copyright_policy_notes TEXT,
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
  health_status TEXT NOT NULL DEFAULT 'unknown' CHECK(health_status IN (
    'ok',
    'empty_archive',
    'tls_error',
    'nx_domain',
    'suspended',
    'forbidden',
    'changed_structure',
    'manual_review_needed',
    'unknown'
  )),
  last_checked_at INTEGER,
  UNIQUE(canonical_url, source_type)
) STRICT;

CREATE TABLE aircraft_origin_routes (
  id INTEGER PRIMARY KEY,
  aircraft_type_pattern TEXT NOT NULL,
  normalized_pattern TEXT NOT NULL,
  manufacturer TEXT NOT NULL,
  state_of_design_country_id INTEGER NOT NULL REFERENCES countries(id),
  state_of_manufacture_country_id INTEGER REFERENCES countries(id),
  expected_authority_id INTEGER REFERENCES authorities(id),
  expected_source_name TEXT NOT NULL,
  priority INTEGER NOT NULL,
  UNIQUE(normalized_pattern, expected_source_name)
) STRICT;

CREATE INDEX idx_authorities_country ON authorities(country_id);
CREATE INDEX idx_regional_body_members_country ON regional_body_members(country_id);
CREATE INDEX idx_aircraft_routes_design_country
  ON aircraft_origin_routes(state_of_design_country_id);
CREATE INDEX idx_aircraft_routes_manufacture_country
  ON aircraft_origin_routes(state_of_manufacture_country_id);
CREATE INDEX idx_aircraft_routes_authority
  ON aircraft_origin_routes(expected_authority_id);

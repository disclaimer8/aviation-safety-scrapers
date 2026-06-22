# Aviation Coverage Control Plane Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone Go control plane that owns aviation-source coverage metadata, seeds every ISO 3166 country, imports ICAO AIA and RAIO/ICM data safely, preserves curated overrides, validates invariants, and exports deterministic JSON.

**Architecture:** A self-contained `control-plane` Go module uses pure-Go SQLite, embedded versioned migrations, embedded reviewable JSON seeds, and a standard-library CLI. Importers share a bounded HTTP/snapshot/run framework but keep source-specific parsing and apply logic isolated; canonical data is updated transactionally only after staging and validation.

**Tech Stack:** Go 1.24+, `modernc.org/sqlite`, `golang.org/x/net/html`, standard-library `flag`, `embed`, `net/http`, `database/sql`, `encoding/json`, and `testing`.

---

## Delivery milestones

1. **Schema and seed foundation:** Tasks 1-5 produce a usable seeded database.
2. **Provenance and import framework:** Tasks 6-8 produce safe snapshots, runs, and override-aware writes.
3. **ICAO source importers:** Tasks 9-10 import AIA and RAIO/ICM fixtures and live pages.
4. **Validation, export, CLI, and operations:** Tasks 11-14 complete the release contract.

## File map

```text
control-plane/
  go.mod
  go.sum
  cmd/aviation-coverage/main.go
  internal/app/app.go
  internal/config/config.go
  internal/database/database.go
  internal/migrations/migrations.go
  internal/migrations/sql/001_core.sql
  internal/migrations/sql/002_pipeline.sql
  internal/migrations/sql/003_provenance.sql
  internal/model/enums.go
  internal/model/types.go
  internal/seed/seed.go
  internal/seed/data/iso3166.json
  internal/seed/data/country_overlays.json
  internal/seed/data/regional_bodies.json
  internal/seed/data/sources.json
  internal/seed/data/aircraft_origin_routes.json
  internal/fetch/fetch.go
  internal/provenance/store.go
  internal/effective/authorities.go
  internal/importer/common/result.go
  internal/importer/aia/parse.go
  internal/importer/aia/import.go
  internal/importer/raio/parse.go
  internal/importer/raio/import.go
  internal/validation/validation.go
  internal/export/export.go
  internal/atomicfile/write.go
  fixtures/icao/aia.html
  fixtures/icao/raio.html
  README.md
```

Tests live beside the code as `*_test.go`. Test fixtures never access the network.

### Stable public Go contracts

Use these signatures throughout the implementation:

```go
package migrations

func Apply(ctx context.Context, db *sql.DB) error
```

```go
package seed

func Apply(ctx context.Context, db *sql.DB) (Stats, error)
```

```go
package fetch

type Request struct {
	URL       string
	UserAgent string
	Timeout   time.Duration
	MaxBytes  int64
	Retries   int
}

type Response struct {
	FinalURL    string
	StatusCode  int
	ContentType string
	ETag        string
	LastModified string
	Body        []byte
	FetchedAt   time.Time
}

func Get(ctx context.Context, client *http.Client, req Request) (Response, error)
```

```go
package provenance

type SnapshotInput struct {
	SourceID     int64
	SourceURL    string
	FinalURL     string
	StatusCode   int
	ContentType  string
	ETag         string
	LastModified string
	FetchedAt    time.Time
	Body         []byte
}

func PutSnapshot(ctx context.Context, db DBTX, in SnapshotInput) (Snapshot, bool, error)
func StartRun(ctx context.Context, db DBTX, importer, sourceURL string) (Run, error)
func FinishRun(ctx context.Context, db DBTX, runID int64, result RunResult) error
```

```go
package aia

func Parse(r io.Reader) ([]Record, error)
func Import(ctx context.Context, db *sql.DB, input importer.Input) (common.Result, error)
```

```go
package raio

func Parse(r io.Reader) ([]BodyRecord, error)
func Import(ctx context.Context, db *sql.DB, input importer.Input) (common.Result, error)
```

```go
package validation

func Run(ctx context.Context, db *sql.DB, opts Options) Report
```

```go
package export

func Build(ctx context.Context, db *sql.DB, generatedAt time.Time) (Document, error)
func WriteJSON(ctx context.Context, db *sql.DB, output string, generatedAt time.Time) error
```

## Task 1: Bootstrap the Go module and database connection

**Files:**

- Create: `control-plane/go.mod`
- Create: `control-plane/internal/database/database.go`
- Create: `control-plane/internal/database/database_test.go`
- Create: `control-plane/internal/config/config.go`

- [ ] **Step 1: Create the module and add dependencies**

Run:

```bash
mkdir -p control-plane
cd control-plane
go mod init github.com/denyskolomiiets/aviation-safety-scrapers/control-plane
go get modernc.org/sqlite@latest
go get golang.org/x/net@latest
```

Expected: `go.mod` and `go.sum` exist, with no changes outside `control-plane/`.

- [ ] **Step 2: Write the failing database test**

```go
package database

import (
	"context"
	"path/filepath"
	"testing"
)

func TestOpenEnablesForeignKeysAndWAL(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "coverage.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	var foreignKeys int
	if err := db.QueryRowContext(context.Background(), "PRAGMA foreign_keys").Scan(&foreignKeys); err != nil {
		t.Fatal(err)
	}
	if foreignKeys != 1 {
		t.Fatalf("foreign_keys=%d, want 1", foreignKeys)
	}

	var mode string
	if err := db.QueryRowContext(context.Background(), "PRAGMA journal_mode").Scan(&mode); err != nil {
		t.Fatal(err)
	}
	if mode != "wal" {
		t.Fatalf("journal_mode=%q, want wal", mode)
	}
}
```

- [ ] **Step 3: Verify the test fails**

Run:

```bash
go test ./internal/database -run TestOpenEnablesForeignKeysAndWAL -v
```

Expected: compile failure because `Open` does not exist.

- [ ] **Step 4: Implement the database connection**

```go
package database

import (
	"database/sql"
	"fmt"
	"net/url"

	_ "modernc.org/sqlite"
)

func Open(path string) (*sql.DB, error) {
	q := url.Values{}
	q.Add("_pragma", "foreign_keys(1)")
	q.Add("_pragma", "journal_mode(WAL)")
	q.Add("_pragma", "busy_timeout(10000)")
	dsn := "file:" + path + "?" + q.Encode()

	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open sqlite: %w", err)
	}
	db.SetMaxOpenConns(1)
	if err := db.Ping(); err != nil {
		db.Close()
		return nil, fmt.Errorf("ping sqlite: %w", err)
	}
	return db, nil
}
```

Create `config.go`:

```go
package config

import "time"

const (
	DefaultAIAURL  = "https://www.icao.int/safety/AIG/AIA"
	DefaultRAIOURL = "https://www.icao.int/safety/regional-safety-cooperation/List-of-RAIOs-and-ICMs"
	DefaultMaxBody = int64(8 << 20)
)

type HTTP struct {
	UserAgent string
	Timeout   time.Duration
	MaxBytes  int64
	Retries   int
}

func DefaultHTTP() HTTP {
	return HTTP{
		UserAgent: "aviation-coverage-control-plane/1.0 (+https://github.com/denyskolomiiets/aviation-safety-scrapers)",
		Timeout:   30 * time.Second,
		MaxBytes:  DefaultMaxBody,
		Retries:   2,
	}
}
```

- [ ] **Step 5: Run tests**

Run:

```bash
go test ./internal/database ./internal/config
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add control-plane/go.mod control-plane/go.sum control-plane/internal/database control-plane/internal/config
git commit -m "feat(control-plane): bootstrap Go database module"
```

## Task 2: Implement embedded, idempotent migrations

**Files:**

- Create: `control-plane/internal/migrations/migrations.go`
- Create: `control-plane/internal/migrations/migrations_test.go`
- Create: `control-plane/internal/migrations/sql/001_core.sql`
- Create: `control-plane/internal/migrations/sql/002_pipeline.sql`
- Create: `control-plane/internal/migrations/sql/003_provenance.sql`

- [ ] **Step 1: Write the failing migration tests**

```go
package migrations

import (
	"context"
	"path/filepath"
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/database"
)

func TestApplyCreatesCompleteSchemaAndIsIdempotent(t *testing.T) {
	db, err := database.Open(filepath.Join(t.TempDir(), "coverage.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	ctx := context.Background()
	if err := Apply(ctx, db); err != nil {
		t.Fatal(err)
	}
	if err := Apply(ctx, db); err != nil {
		t.Fatalf("second apply: %v", err)
	}

	required := []string{
		"countries", "authorities", "regional_bodies", "regional_body_members",
		"sources", "events", "reports", "event_source_links",
		"investigation_participants", "aircraft_origin_routes",
		"crawl_jobs", "crawl_errors", "import_runs", "source_snapshots",
		"staged_authorities", "staged_regional_bodies", "field_overrides",
		"import_conflicts", "authority_requests",
	}
	for _, table := range required {
		var got string
		err := db.QueryRowContext(ctx,
			`SELECT name FROM sqlite_master WHERE type='table' AND name=?`, table,
		).Scan(&got)
		if err != nil || got != table {
			t.Fatalf("missing table %s: %v", table, err)
		}
	}

	var count int
	if err := db.QueryRowContext(ctx, `SELECT COUNT(*) FROM schema_migrations`).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 3 {
		t.Fatalf("migration count=%d, want 3", count)
	}
}

func TestSchemaRejectsInvalidCountryEnumsAndScores(t *testing.T) {
	db, _ := database.Open(filepath.Join(t.TempDir(), "coverage.db"))
	defer db.Close()
	if err := Apply(context.Background(), db); err != nil {
		t.Fatal(err)
	}
	_, err := db.Exec(`INSERT INTO countries
		(iso2, iso3, name, region, policy_status, coverage_status, coverage_score,
		 effort_score, expected_records, expected_source_quality, priority_score)
		VALUES ('ZZ','ZZZ','Invalid','Test','bad','unknown',6,0,0,1,0)`)
	if err == nil {
		t.Fatal("expected CHECK constraint failure")
	}
}
```

- [ ] **Step 2: Verify the migration tests fail**

Run:

```bash
go test ./internal/migrations -v
```

Expected: compile failure because `Apply` does not exist.

- [ ] **Step 3: Create `001_core.sql`**

The migration must create:

```sql
CREATE TABLE countries (
  id INTEGER PRIMARY KEY,
  iso2 TEXT NOT NULL UNIQUE CHECK(length(iso2)=2),
  iso3 TEXT NOT NULL UNIQUE CHECK(length(iso3)=3),
  name TEXT NOT NULL,
  region TEXT NOT NULL,
  policy_status TEXT NOT NULL CHECK(policy_status IN ('allowed','indirect_public_only','excluded')),
  coverage_status TEXT NOT NULL CHECK(coverage_status IN (
    'direct_public_archive','delegated_to_foreign_authority','regional_raio',
    'official_contact_only','source_exists_unstable','no_public_archive',
    'policy_excluded','unknown'
  )),
  coverage_score INTEGER NOT NULL CHECK(coverage_score BETWEEN 0 AND 5),
  effort_score INTEGER NOT NULL CHECK(effort_score BETWEEN 1 AND 5),
  expected_records INTEGER NOT NULL DEFAULT 0 CHECK(expected_records >= 0),
  expected_source_quality INTEGER NOT NULL DEFAULT 1 CHECK(expected_source_quality BETWEEN 1 AND 5),
  priority_score REAL NOT NULL DEFAULT 0,
  country_group TEXT CHECK(country_group IN ('A','B','C1','C2','C3','D') OR country_group IS NULL),
  refresh_cadence TEXT,
  last_checked_at INTEGER,
  notes TEXT
);

CREATE TABLE authorities (
  id INTEGER PRIMARY KEY,
  country_id INTEGER NOT NULL REFERENCES countries(id),
  normalized_name TEXT NOT NULL,
  name TEXT NOT NULL,
  type TEXT NOT NULL CHECK(type IN (
    'national_aai','caa','ministry','regional_raio','foreign_aai',
    'manufacturer_state_aai','operator_state_aai','registry_state_aai'
  )),
  website_url TEXT,
  archive_url TEXT,
  contact_email TEXT,
  contact_phone TEXT,
  source_url TEXT NOT NULL,
  source_name TEXT NOT NULL,
  has_public_archive INTEGER CHECK(has_public_archive IN (0,1) OR has_public_archive IS NULL),
  status TEXT NOT NULL DEFAULT 'unknown' CHECK(status IN (
    'ok','empty_archive','tls_error','nx_domain','suspended','forbidden',
    'changed_structure','manual_review_needed','unknown'
  )),
  source_snapshot_id INTEGER,
  last_checked_at INTEGER,
  notes TEXT,
  UNIQUE(country_id, normalized_name, type)
);

CREATE TABLE regional_bodies (
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  body_class TEXT NOT NULL CHECK(body_class IN ('raio','icm','regional_body')),
  website_url TEXT,
  source_url TEXT NOT NULL,
  notes TEXT
);

CREATE TABLE regional_body_members (
  regional_body_id INTEGER NOT NULL REFERENCES regional_bodies(id) ON DELETE CASCADE,
  country_id INTEGER NOT NULL REFERENCES countries(id),
  role TEXT NOT NULL,
  source_url TEXT NOT NULL,
  PRIMARY KEY(regional_body_id, country_id, role)
);

CREATE TABLE sources (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  url TEXT NOT NULL,
  canonical_url TEXT NOT NULL,
  source_type TEXT NOT NULL CHECK(source_type IN (
    'official_aai','official_foreign_accredited_rep','icao_elibrary','regulator',
    'ministry','operator','manufacturer','regional_body','trusted_index','media','wayback'
  )),
  source_tier INTEGER NOT NULL CHECK(source_tier BETWEEN 1 AND 6),
  robots_policy TEXT,
  copyright_policy_notes TEXT,
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
  health_status TEXT NOT NULL DEFAULT 'unknown' CHECK(health_status IN (
    'ok','empty_archive','tls_error','nx_domain','suspended','forbidden',
    'changed_structure','manual_review_needed','unknown'
  )),
  last_checked_at INTEGER,
  UNIQUE(canonical_url, source_type)
);

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
);
```

- [ ] **Step 4: Create `002_pipeline.sql`**

Create the requested `events`, `reports`, `event_source_links`, `investigation_participants`, `crawl_jobs`, and `crawl_errors` tables with these exact column groups:

- `events`: ID, date/date precision, occurrence country, location/coordinates, aircraft identity, operator/flight, fatalities/injuries, event type, investigation status, confidence score, `dedup_status`, `needs_official_confirmation`, creation/update timestamps.
- `reports`: event/source FKs, report type, title/language, original/archived/PDF URLs, publication/access dates, checksum/local path, source tier, extraction status, copyright status, notes.
- `event_source_links`: event/source FKs, native source ID/URL, optional matched event, confidence, reason, creation time.
- `investigation_participants`: event/country/optional authority FKs, exact approved role enum, source URL, notes.
- `crawl_jobs`: source/country FKs, exact approved job type/status enums, start/finish/error/statistics/creation fields.
- `crawl_errors`: job FK, URL, exact approved error type enum, message, creation time.

Use these required defaults and constraints:

```sql
CHECK(confidence_score BETWEEN 0 AND 100)
CHECK(match_confidence BETWEEN 0 AND 100)
CHECK(needs_official_confirmation IN (0,1))
CHECK(dedup_status IN ('unreviewed','auto_merged','soft_linked','manual_review','distinct'))
CHECK(copyright_status IN ('official_public','metadata_only','unknown','do_not_store_fulltext'))
```

Add indexes:

```sql
CREATE INDEX idx_events_date_registration_country
  ON events(date, aircraft_registration, occurrence_country_id);
CREATE INDEX idx_events_fallback_match
  ON events(date, aircraft_type, operator_name, fatalities);
CREATE INDEX idx_reports_event ON reports(event_id);
CREATE INDEX idx_crawl_jobs_status_type ON crawl_jobs(status, job_type);
CREATE INDEX idx_crawl_errors_job ON crawl_errors(crawl_job_id);
```

- [ ] **Step 5: Create `003_provenance.sql`**

Create `import_runs`, `source_snapshots`, both staging tables, overrides, conflicts, and authority requests. Include:

```sql
CREATE UNIQUE INDEX idx_snapshots_source_checksum
  ON source_snapshots(source_id, checksum);
CREATE UNIQUE INDEX idx_active_field_override
  ON field_overrides(entity_type, entity_id, field_name)
  WHERE active = 1;
CREATE INDEX idx_import_conflicts_open
  ON import_conflicts(review_status)
  WHERE review_status = 'open';
```

After `source_snapshots` exists, add the authority snapshot reference using:

```sql
CREATE TRIGGER authorities_snapshot_guard
BEFORE UPDATE OF source_snapshot_id ON authorities
WHEN NEW.source_snapshot_id IS NOT NULL
 AND NOT EXISTS (SELECT 1 FROM source_snapshots WHERE id=NEW.source_snapshot_id)
BEGIN
  SELECT RAISE(ABORT, 'unknown authority source snapshot');
END;
```

- [ ] **Step 6: Implement migration discovery and transactions**

```go
package migrations

import (
	"context"
	"database/sql"
	"embed"
	"fmt"
	"io/fs"
	"sort"
	"strconv"
	"strings"
)

//go:embed sql/*.sql
var files embed.FS

func Apply(ctx context.Context, db *sql.DB) error {
	if _, err := db.ExecContext(ctx, `CREATE TABLE IF NOT EXISTS schema_migrations (
		version INTEGER PRIMARY KEY,
		name TEXT NOT NULL,
		applied_at INTEGER NOT NULL
	)`); err != nil {
		return err
	}
	names, err := fs.Glob(files, "sql/*.sql")
	if err != nil {
		return err
	}
	sort.Strings(names)
	for _, name := range names {
		base := strings.TrimSuffix(strings.TrimPrefix(name, "sql/"), ".sql")
		parts := strings.SplitN(base, "_", 2)
		version, err := strconv.Atoi(parts[0])
		if err != nil {
			return fmt.Errorf("migration %s: %w", name, err)
		}
		var exists int
		if err := db.QueryRowContext(ctx,
			`SELECT COUNT(*) FROM schema_migrations WHERE version=?`, version,
		).Scan(&exists); err != nil {
			return err
		}
		if exists == 1 {
			continue
		}
		body, err := files.ReadFile(name)
		if err != nil {
			return err
		}
		tx, err := db.BeginTx(ctx, nil)
		if err != nil {
			return err
		}
		if _, err = tx.ExecContext(ctx, string(body)); err == nil {
			_, err = tx.ExecContext(ctx,
				`INSERT INTO schema_migrations(version,name,applied_at)
				 VALUES(?,?,unixepoch('subsec')*1000)`,
				version, base,
			)
		}
		if err != nil {
			tx.Rollback()
			return fmt.Errorf("apply %s: %w", name, err)
		}
		if err := tx.Commit(); err != nil {
			return err
		}
	}
	return nil
}
```

- [ ] **Step 7: Run tests and inspect foreign keys**

Run:

```bash
go test ./internal/migrations -v
go test ./...
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add control-plane/internal/migrations
git commit -m "feat(control-plane): add canonical SQLite schema"
```

## Task 3: Define domain enums and normalization helpers

**Files:**

- Create: `control-plane/internal/model/enums.go`
- Create: `control-plane/internal/model/types.go`
- Create: `control-plane/internal/model/model_test.go`

- [ ] **Step 1: Write failing enum and normalization tests**

```go
package model

import "testing"

func TestNormalizeAuthorityName(t *testing.T) {
	got := NormalizeName("  Bureau d’Enquêtes  ET   d'Analyses ")
	want := "bureau d'enquetes et d'analyses"
	if got != want {
		t.Fatalf("got %q want %q", got, want)
	}
}

func TestPriorityScore(t *testing.T) {
	if got := PriorityScore(120, 4, 3); got != 160 {
		t.Fatalf("got %v want 160", got)
	}
}

func TestSourceTierAllowsType(t *testing.T) {
	if !SourceTierAllowsType(5, SourceTrustedIndex) {
		t.Fatal("tier 5 should allow trusted_index")
	}
	if SourceTierAllowsType(1, SourceMedia) {
		t.Fatal("tier 1 must reject media")
	}
}
```

- [ ] **Step 2: Verify tests fail**

Run:

```bash
go test ./internal/model -v
```

Expected: compile failure for missing helpers.

- [ ] **Step 3: Implement typed constants and helpers**

Define constants for every enum in the approved design. Implement:

```go
func NormalizeName(s string) string {
	s = strings.TrimSpace(strings.ToLower(s))
	replacer := strings.NewReplacer("’", "'", "‘", "'", "–", "-", "—", "-")
	s = replacer.Replace(s)
	s = norm.NFD.String(s)
	s = strings.Map(func(r rune) rune {
		if unicode.Is(unicode.Mn, r) {
			return -1
		}
		return r
	}, s)
	return strings.Join(strings.Fields(s), " ")
}

func PriorityScore(expectedRecords, quality, effort int) float64 {
	if effort <= 0 {
		return 0
	}
	return float64(expectedRecords*quality) / float64(effort)
}

func SourceTierAllowsType(tier int, typ SourceType) bool {
	switch tier {
	case 1:
		return typ == SourceOfficialAAI
	case 2:
		return typ == SourceOfficialForeignAccreditedRep
	case 3:
		return typ == SourceICAOELibrary
	case 4:
		return typ == SourceRegulator || typ == SourceMinistry ||
			typ == SourceOperator || typ == SourceManufacturer || typ == SourceRegionalBody
	case 5:
		return typ == SourceTrustedIndex
	case 6:
		return typ == SourceMedia
	default:
		return false
	}
}
```

Use `golang.org/x/text/unicode/norm`; add it with:

```bash
go get golang.org/x/text@latest
```

- [ ] **Step 4: Run tests**

Run:

```bash
go test ./internal/model -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add control-plane/go.mod control-plane/go.sum control-plane/internal/model
git commit -m "feat(control-plane): define coverage domain model"
```

## Task 4: Seed all ISO countries and country overlays

**Files:**

- Create: `control-plane/internal/seed/data/iso3166.json`
- Create: `control-plane/internal/seed/data/country_overlays.json`
- Create: `control-plane/internal/seed/seed.go`
- Create: `control-plane/internal/seed/seed_test.go`

- [ ] **Step 1: Add reviewable seed files**

`iso3166.json` contains the 249 ISO 3166-1 assigned country entries in this exact shape:

```json
[
  {"iso2":"AD","iso3":"AND","name":"Andorra","region":"Europe"},
  {"iso2":"AE","iso3":"ARE","name":"United Arab Emirates","region":"Asia"},
  {"iso2":"AF","iso3":"AFG","name":"Afghanistan","region":"Asia"}
]
```

Continue alphabetically through `ZW/ZWE/Zimbabwe`. Generate the file once from a pinned ISO dataset, commit the resulting JSON, and make runtime seeding independent of network access. The acceptance test below requires exactly 249 unique ISO2 and ISO3 entries.

`country_overlays.json` uses:

```json
[
  {
    "iso2":"AD",
    "group":"A",
    "policy_status":"allowed",
    "coverage_status":"delegated_to_foreign_authority",
    "coverage_score":3,
    "effort_score":2,
    "expected_records":5,
    "expected_source_quality":4,
    "refresh_cadence":"quarterly",
    "notes":"Route through Spain CIAIAC and France BEA."
  },
  {
    "iso2":"AF",
    "group":"B",
    "policy_status":"excluded",
    "coverage_status":"policy_excluded",
    "coverage_score":2,
    "effort_score":5,
    "expected_records":10,
    "expected_source_quality":3,
    "refresh_cadence":"quarterly",
    "notes":"No direct acquisition; public non-sanctioned official sources only."
  },
  {
    "iso2":"PA",
    "group":"D",
    "policy_status":"allowed",
    "coverage_status":"source_exists_unstable",
    "coverage_score":4,
    "effort_score":3,
    "expected_records":80,
    "expected_source_quality":5,
    "refresh_cadence":"weekly",
    "notes":"UPIA retry/backoff, Wayback, PDF discovery, ICAO and aircraft-origin routes."
  }
]
```

Include every approved A/B/C1/C2/C3/D country. Countries without overlays default to `allowed`, `unknown`, coverage score `0`, effort `3`, expected records `0`, expected quality `1`.

- [ ] **Step 2: Write failing seed tests**

```go
func TestApplySeedsAllCountriesAndIsIdempotent(t *testing.T) {
	db := testDB(t)
	ctx := context.Background()
	first, err := Apply(ctx, db)
	if err != nil {
		t.Fatal(err)
	}
	second, err := Apply(ctx, db)
	if err != nil {
		t.Fatal(err)
	}
	if first.Countries != 249 || second.Countries != 249 {
		t.Fatalf("country stats first=%d second=%d", first.Countries, second.Countries)
	}
	var count, iso2, iso3 int
	db.QueryRow(`SELECT COUNT(*), COUNT(DISTINCT iso2), COUNT(DISTINCT iso3) FROM countries`).
		Scan(&count, &iso2, &iso3)
	if count != 249 || iso2 != 249 || iso3 != 249 {
		t.Fatalf("counts=%d/%d/%d", count, iso2, iso3)
	}
}

func TestPolicyExcludedAndPriorityOverlay(t *testing.T) {
	db := testDB(t)
	if _, err := Apply(context.Background(), db); err != nil {
		t.Fatal(err)
	}
	var policy, coverage string
	db.QueryRow(`SELECT policy_status, coverage_status FROM countries WHERE iso2='AF'`).
		Scan(&policy, &coverage)
	if policy != "excluded" || coverage != "policy_excluded" {
		t.Fatalf("AF policy=%s coverage=%s", policy, coverage)
	}
	var score float64
	db.QueryRow(`SELECT priority_score FROM countries WHERE iso2='PA'`).Scan(&score)
	if score != float64(80*5)/3 {
		t.Fatalf("PA priority=%v", score)
	}
}
```

- [ ] **Step 3: Verify tests fail**

Run:

```bash
go test ./internal/seed -run 'TestApplySeedsAllCountriesAndIsIdempotent|TestPolicyExcludedAndPriorityOverlay' -v
```

Expected: compile failure because `Apply` does not exist.

- [ ] **Step 4: Implement embedded seed application**

Use `//go:embed data/*.json`. Parse into typed structs, validate all entries before opening a transaction, then use:

```sql
INSERT INTO countries (
  iso2, iso3, name, region, policy_status, coverage_status,
  coverage_score, effort_score, expected_records, expected_source_quality,
  priority_score, country_group, refresh_cadence, notes
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(iso2) DO UPDATE SET
  iso3=excluded.iso3,
  name=excluded.name,
  region=excluded.region,
  policy_status=excluded.policy_status,
  coverage_status=excluded.coverage_status,
  coverage_score=excluded.coverage_score,
  effort_score=excluded.effort_score,
  expected_records=excluded.expected_records,
  expected_source_quality=excluded.expected_source_quality,
  priority_score=excluded.priority_score,
  country_group=excluded.country_group,
  refresh_cadence=excluded.refresh_cadence,
  notes=excluded.notes
```

Return:

```go
type Stats struct {
	Countries            int
	RegionalBodies       int
	RegionalMembers      int
	Sources              int
	AircraftOriginRoutes int
}
```

- [ ] **Step 5: Run tests**

Run:

```bash
go test ./internal/seed -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add control-plane/internal/seed
git commit -m "feat(control-plane): seed ISO countries and coverage policy"
```

## Task 5: Seed regional bodies, sources, and aircraft-origin routes

**Files:**

- Create: `control-plane/internal/seed/data/regional_bodies.json`
- Create: `control-plane/internal/seed/data/sources.json`
- Create: `control-plane/internal/seed/data/aircraft_origin_routes.json`
- Modify: `control-plane/internal/seed/seed.go`
- Modify: `control-plane/internal/seed/seed_test.go`

- [ ] **Step 1: Write failing mapping tests**

```go
func TestRequiredRegionalMappings(t *testing.T) {
	db := testDB(t)
	if _, err := Apply(context.Background(), db); err != nil {
		t.Fatal(err)
	}
	cases := map[string]int{"ECCAA": 5, "BAGAIA": 7, "IAC": 8}
	for code, want := range cases {
		var got int
		err := db.QueryRow(`
			SELECT COUNT(*) FROM regional_body_members m
			JOIN regional_bodies b ON b.id=m.regional_body_id
			WHERE b.code=?`, code).Scan(&got)
		if err != nil || got != want {
			t.Fatalf("%s members=%d want=%d err=%v", code, got, want, err)
		}
	}
}

func TestAircraftOriginAndSourceSeeds(t *testing.T) {
	db := testDB(t)
	if _, err := Apply(context.Background(), db); err != nil {
		t.Fatal(err)
	}
	var source string
	db.QueryRow(`SELECT expected_source_name FROM aircraft_origin_routes
		WHERE normalized_pattern='boeing'`).Scan(&source)
	if source != "NTSB" {
		t.Fatalf("boeing source=%q", source)
	}
	var tier int
	db.QueryRow(`SELECT source_tier FROM sources WHERE name='ICAO e-Library Final Reports'`).
		Scan(&tier)
	if tier != 3 {
		t.Fatalf("ICAO tier=%d", tier)
	}
}
```

- [ ] **Step 2: Verify tests fail**

Run:

```bash
go test ./internal/seed -run 'TestRequiredRegionalMappings|TestAircraftOriginAndSourceSeeds' -v
```

Expected: FAIL because mappings are absent.

- [ ] **Step 3: Add complete curated JSON seeds**

`regional_bodies.json` must contain:

- ECCAA with Dominica, Grenada, Saint Kitts and Nevis, Saint Lucia, Saint Vincent and the Grenadines;
- BAGAIA with Cabo Verde, Gambia, Ghana, Guinea, Liberia, Nigeria, Sierra Leone;
- IAC with Armenia, Azerbaijan, Belarus, Kazakhstan, Kyrgyzstan, Tajikistan, Turkmenistan, Russian Federation;
- ARCM-MENA, ARCM-SAM, ENCASIA, and GRIAA as ICM/cooperation records from ICAO.

Use ISO2 values in member arrays, never country-name joins.

`sources.json` must seed the official ICAO AIA and RAIO pages, ICAO e-Library, NTSB, BEA, ATSB, AAIB, TSB, TAIC, SKYbrary, ASN, B3A, and Aviation Herald with the approved tiers and rights notes.

`aircraft_origin_routes.json` must contain one record per listed manufacturer family, for example:

```json
{
  "patterns":["Boeing","Cessna","Piper","Beechcraft","Gulfstream","Lycoming","Continental"],
  "manufacturer":"United States design/manufacture",
  "state_of_design_iso2":"US",
  "state_of_manufacture_iso2":"US",
  "expected_source_name":"NTSB",
  "priority":100
}
```

- [ ] **Step 4: Extend `seed.Apply`**

Resolve all ISO2 references inside the same transaction. Abort before commit if a country or source reference is missing. Use UPSERTs keyed by body code, canonical source URL/type, and normalized pattern/source name.

- [ ] **Step 5: Run tests**

Run:

```bash
go test ./internal/seed -v
go test ./...
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add control-plane/internal/seed
git commit -m "feat(control-plane): seed regional and aircraft routing metadata"
```

## Task 6: Build the bounded HTTP fetcher

**Files:**

- Create: `control-plane/internal/fetch/fetch.go`
- Create: `control-plane/internal/fetch/fetch_test.go`

- [ ] **Step 1: Write failing transport tests**

Test with `httptest.Server`:

```go
func TestGetRetriesServerErrorsAndReturnsMetadata(t *testing.T) {
	var attempts atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("User-Agent") != "coverage-test/1" {
			t.Errorf("unexpected user agent %q", r.Header.Get("User-Agent"))
		}
		if attempts.Add(1) < 3 {
			http.Error(w, "temporary", http.StatusServiceUnavailable)
			return
		}
		w.Header().Set("Content-Type", "text/html")
		w.Header().Set("ETag", `"abc"`)
		io.WriteString(w, "<html>ok</html>")
	}))
	defer srv.Close()

	got, err := Get(context.Background(), srv.Client(), Request{
		URL: srv.URL, UserAgent: "coverage-test/1",
		Timeout: time.Second, MaxBytes: 1024, Retries: 2,
	})
	if err != nil {
		t.Fatal(err)
	}
	if string(got.Body) != "<html>ok</html>" || attempts.Load() != 3 {
		t.Fatalf("body=%q attempts=%d", got.Body, attempts.Load())
	}
}

func TestGetRejectsOversizedBody(t *testing.T) {
	// Server returns 2048 bytes; MaxBytes is 128.
	// Assert errors.Is(err, ErrBodyTooLarge).
}
```

- [ ] **Step 2: Verify tests fail**

Run:

```bash
go test ./internal/fetch -v
```

Expected: compile failure because `Get` is missing.

- [ ] **Step 3: Implement the fetcher**

Requirements:

- only `http` and `https`;
- maximum five redirects;
- context timeout per attempt;
- retries only network errors, 429, and 5xx;
- exponential waits of 250ms, 500ms;
- `io.LimitReader(MaxBytes+1)`;
- error on body overflow;
- preserve final URL and response metadata;
- reject non-2xx final responses.

Expose sentinel errors:

```go
var (
	ErrBodyTooLarge = errors.New("response body exceeds limit")
	ErrUnsupportedScheme = errors.New("unsupported URL scheme")
)
```

- [ ] **Step 4: Run tests**

Run:

```bash
go test ./internal/fetch -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add control-plane/internal/fetch
git commit -m "feat(control-plane): add bounded resilient HTTP fetcher"
```

## Task 7: Implement immutable snapshots and import runs

**Files:**

- Create: `control-plane/internal/provenance/store.go`
- Create: `control-plane/internal/provenance/store_test.go`
- Create: `control-plane/internal/importer/common/result.go`

- [ ] **Step 1: Write failing snapshot/run tests**

```go
func TestPutSnapshotIsContentIdempotent(t *testing.T) {
	db := testDB(t)
	sourceID := insertSource(t, db)
	in := SnapshotInput{
		SourceID: sourceID, SourceURL: "https://example.test/aia",
		FinalURL: "https://example.test/aia", StatusCode: 200,
		ContentType: "text/html", FetchedAt: time.Unix(100, 0),
		Body: []byte("<html>AIA</html>"),
	}
	first, created, err := PutSnapshot(context.Background(), db, in)
	if err != nil || !created {
		t.Fatalf("first created=%v err=%v", created, err)
	}
	second, created, err := PutSnapshot(context.Background(), db, in)
	if err != nil || created || first.ID != second.ID {
		t.Fatalf("second=%+v created=%v err=%v", second, created, err)
	}
}

func TestRunLifecycle(t *testing.T) {
	db := testDB(t)
	run, err := StartRun(context.Background(), db, "aia", "https://example.test/aia")
	if err != nil {
		t.Fatal(err)
	}
	err = FinishRun(context.Background(), db, run.ID, RunResult{
		Status: "partial", Parsed: 4, Applied: 3, Warnings: 1,
		ErrorSummary: "one unresolved country",
	})
	if err != nil {
		t.Fatal(err)
	}
}
```

- [ ] **Step 2: Verify tests fail**

Run:

```bash
go test ./internal/provenance -v
```

Expected: compile failure for missing functions.

- [ ] **Step 3: Implement snapshot and run storage**

Calculate checksum with:

```go
sum := sha256.Sum256(in.Body)
checksum := hex.EncodeToString(sum[:])
```

Store raw bodies as `BLOB` in the foundation release. `PutSnapshot` uses `INSERT ... ON CONFLICT DO NOTHING`, then selects by source/checksum.

Define:

```go
type DBTX interface {
	ExecContext(context.Context, string, ...any) (sql.Result, error)
	QueryRowContext(context.Context, string, ...any) *sql.Row
}
```

Define common importer result:

```go
type Result struct {
	RunID     int64
	Status    string
	Parsed    int
	Applied   int
	Warnings  int
	Conflicts int
	Unchanged bool
}
```

- [ ] **Step 4: Run tests**

Run:

```bash
go test ./internal/provenance ./internal/importer/common -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add control-plane/internal/provenance control-plane/internal/importer/common
git commit -m "feat(control-plane): persist source snapshots and import runs"
```

## Task 8: Implement curated overrides and effective authority writes

**Files:**

- Create: `control-plane/internal/effective/authorities.go`
- Create: `control-plane/internal/effective/authorities_test.go`

- [ ] **Step 1: Write failing override/conflict tests**

```go
func TestApplyAuthorityPreservesOverrideAndCreatesConflict(t *testing.T) {
	db := testDB(t)
	countryID := countryID(t, db, "SN")
	authorityID := insertAuthority(t, db, countryID, "BEA Senegal", "https://old.example")
	insertOverride(t, db, "authority", authorityID, "website_url", "https://curated.example")

	result, err := ApplyAuthority(context.Background(), db, IncomingAuthority{
		RunID: 7, CountryID: countryID, Name: "BEA Senegal",
		NormalizedName: "bea senegal", Type: "national_aai",
		WebsiteURL: "https://incoming.example", SourceURL: "https://icao.example",
		SourceName: "ICAO AIA", SnapshotID: 9,
	})
	if err != nil {
		t.Fatal(err)
	}
	if result.Conflicts != 1 {
		t.Fatalf("conflicts=%d", result.Conflicts)
	}
	var website string
	db.QueryRow(`SELECT website_url FROM authorities WHERE id=?`, authorityID).Scan(&website)
	if website != "https://curated.example" {
		t.Fatalf("website=%q", website)
	}
}

func TestMissingIncomingValueDoesNotEraseKnownValue(t *testing.T) {
	// Existing contact_email is non-empty and incoming contact_email is empty.
	// Assert value remains and a removal conflict is recorded.
}
```

- [ ] **Step 2: Verify tests fail**

Run:

```bash
go test ./internal/effective -v
```

Expected: compile failure for missing `ApplyAuthority`.

- [ ] **Step 3: Implement effective-value resolution**

Use one transaction supplied by the caller. For each mutable field:

1. Query active `field_overrides`.
2. If override exists, write the override value into the authority.
3. If incoming differs, insert an open conflict.
4. If no override and incoming is non-empty, apply it and set `source_snapshot_id`.
5. If no override and incoming is empty while current is non-empty, preserve current and record `upstream_removal`.

Return:

```go
type ApplyResult struct {
	AuthorityID int64
	Applied     int
	Conflicts   int
}
```

Conflict insertion must be idempotent for run/entity/field/incoming value.

- [ ] **Step 4: Run tests**

Run:

```bash
go test ./internal/effective -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add control-plane/internal/effective
git commit -m "feat(control-plane): preserve curated authority overrides"
```

## Task 9: Parse and import ICAO AIA contacts

**Files:**

- Create: `control-plane/fixtures/icao/aia.html`
- Create: `control-plane/internal/importer/aia/parse.go`
- Create: `control-plane/internal/importer/aia/parse_test.go`
- Create: `control-plane/internal/importer/aia/import.go`
- Create: `control-plane/internal/importer/aia/import_test.go`

- [ ] **Step 1: Add a minimal representative offline fixture**

The fixture must contain:

- Albania with authority and obfuscated email;
- Andorra;
- Angola with website and multiple email categories;
- Anguilla with `Refer to the United Kingdom`;
- Antigua and Barbuda with `See Eastern Caribbean States`;
- Armenia;
- Australia;
- one intentionally malformed contact block.

Preserve the source HTML structure instead of simplifying it to custom test markup.

- [ ] **Step 2: Write failing parser tests**

```go
func TestParseAIAFixture(t *testing.T) {
	f, err := os.Open("../../../fixtures/icao/aia.html")
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	records, err := Parse(f)
	if err != nil {
		t.Fatal(err)
	}
	byCountry := map[string]Record{}
	for _, r := range records {
		byCountry[r.CountryLabel] = r
	}
	if got := byCountry["Angola"]; got.WebsiteURL != "https://initpat.gov.ao" ||
		len(got.Emails) < 2 || got.AuthorityName == "" {
		t.Fatalf("Angola=%+v", got)
	}
	if got := byCountry["Anguilla (DT)"]; got.ReferenceCountry != "United Kingdom" {
		t.Fatalf("Anguilla=%+v", got)
	}
	if got := byCountry["Antigua and Barbuda"]; got.ReferenceBody != "Eastern Caribbean States" {
		t.Fatalf("Antigua=%+v", got)
	}
}
```

- [ ] **Step 3: Verify parser tests fail**

Run:

```bash
go test ./internal/importer/aia -run TestParseAIAFixture -v
```

Expected: compile failure because `Parse` is missing.

- [ ] **Step 4: Implement the AIA parser**

Parse with `golang.org/x/net/html`. Locate the `Accident Investigation Authorities Contact Information` heading, then walk following text/link nodes. A country heading starts a record when normalized text matches the country alias map. Preserve:

```go
type Record struct {
	CountryLabel     string
	AuthorityName    string
	RawContact       string
	Emails           []string
	Phones           []string
	WebsiteURL       string
	ArchiveURL       string
	ReferenceCountry string
	ReferenceBody    string
	UpdatedAt        *time.Time
	Warnings         []string
	Checksum         string
}
```

Email deobfuscation only converts explicit `[at]`/`[dot]` forms. Do not repair unrelated inconsistent addresses.

- [ ] **Step 5: Write failing importer tests**

```go
func TestImportStagesAppliesAndReturnsPartialForUnknownRecord(t *testing.T) {
	db := testDB(t)
	body := fixture(t)
	result, err := Import(context.Background(), db, importer.Input{
		SourceURL: "https://www.icao.int/safety/AIG/AIA",
		Body: body, FetchedAt: time.Unix(100, 0),
	})
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != "partial" || result.Applied < 6 || result.Warnings == 0 {
		t.Fatalf("result=%+v", result)
	}
	var raw string
	db.QueryRow(`SELECT raw_contact FROM staged_authorities
		WHERE country_label='Angola' ORDER BY id DESC LIMIT 1`).Scan(&raw)
	if raw == "" {
		t.Fatal("missing raw contact")
	}
}

func TestImportIdenticalBodyIsUnchanged(t *testing.T) {
	// Import fixture twice. Assert second result.Unchanged and only one snapshot.
}
```

- [ ] **Step 6: Implement transactional AIA import**

`importer.Input` contains either `Body` or enough fetch metadata to construct a snapshot. Import flow:

1. Start run.
2. Resolve the seeded ICAO AIA source.
3. Put snapshot; return `unchanged` if checksum already exists.
4. Parse all records.
5. Insert every record into `staged_authorities`.
6. Resolve exact aliases, dependent territories, and curated references.
7. Call `effective.ApplyAuthority` for resolved authority records.
8. Record unresolved entries as warnings.
9. Commit canonical changes.
10. Finish run as success/partial/failed.

No parser warning is silently discarded.

- [ ] **Step 7: Run tests**

Run:

```bash
go test ./internal/importer/aia -v
go test ./...
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add control-plane/fixtures/icao/aia.html control-plane/internal/importer/aia
git commit -m "feat(control-plane): import ICAO AIA authorities"
```

## Task 10: Parse and import ICAO RAIO/ICM memberships

**Files:**

- Create: `control-plane/fixtures/icao/raio.html`
- Create: `control-plane/internal/importer/raio/parse.go`
- Create: `control-plane/internal/importer/raio/parse_test.go`
- Create: `control-plane/internal/importer/raio/import.go`
- Create: `control-plane/internal/importer/raio/import_test.go`

- [ ] **Step 1: Add a representative RAIO fixture**

Include BAGAIA, IAC, ARCM-MENA, ARCM-SAM, ENCASIA, and GRIAA from the official page, preserving semicolon/comma inconsistencies and observer text.

- [ ] **Step 2: Write failing parser tests**

```go
func TestParseRAIOAndICMSections(t *testing.T) {
	f, _ := os.Open("../../../fixtures/icao/raio.html")
	defer f.Close()
	records, err := Parse(f)
	if err != nil {
		t.Fatal(err)
	}
	byCode := map[string]BodyRecord{}
	for _, r := range records {
		byCode[r.Code] = r
	}
	if byCode["BAGAIA"].Class != "raio" || len(byCode["BAGAIA"].Members) != 7 {
		t.Fatalf("BAGAIA=%+v", byCode["BAGAIA"])
	}
	if byCode["ARCM-MENA"].Class != "icm" {
		t.Fatalf("ARCM-MENA=%+v", byCode["ARCM-MENA"])
	}
	if len(byCode["ARCM-SAM"].Observers) == 0 {
		t.Fatal("expected ARCM-SAM observers")
	}
}
```

- [ ] **Step 3: Verify parser tests fail**

Run:

```bash
go test ./internal/importer/raio -run TestParseRAIOAndICMSections -v
```

Expected: compile failure because `Parse` is missing.

- [ ] **Step 4: Implement RAIO parser**

Define:

```go
type BodyRecord struct {
	Code        string
	Name        string
	Class       string
	Region      string
	Members     []string
	Observers   []string
	WebsiteURL  string
	Warnings    []string
	Checksum    string
}
```

Parse RAIO and ICM sections independently. Split member lists on commas and semicolons only after removing observer clauses. Preserve unresolvable labels as warnings.

- [ ] **Step 5: Write failing importer tests**

```go
func TestImportRAIOPreservesCuratedECCAAAndAppliesICAOMembers(t *testing.T) {
	db := testDB(t)
	before := membershipCount(t, db, "ECCAA")
	result, err := Import(context.Background(), db, importer.Input{
		SourceURL: "https://www.icao.int/safety/regional-safety-cooperation/List-of-RAIOs-and-ICMs",
		Body: fixture(t), FetchedAt: time.Unix(100, 0),
	})
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != "success" && result.Status != "partial" {
		t.Fatalf("result=%+v", result)
	}
	if got := membershipCount(t, db, "ECCAA"); got != before {
		t.Fatalf("ECCAA changed from %d to %d", before, got)
	}
	if got := membershipCount(t, db, "BAGAIA"); got != 7 {
		t.Fatalf("BAGAIA=%d", got)
	}
}
```

- [ ] **Step 6: Implement transactional RAIO import**

Rules:

- Upsert body identity and website.
- Upsert source-derived memberships.
- Do not delete curated memberships absent from ICAO.
- Store observers with role `observer`.
- Set `coverage_status='regional_raio'` only for RAIO members whose current coverage is `unknown`, `official_contact_only`, or `no_public_archive`.
- ICM membership never changes coverage without a curated rule.
- Unknown labels create warnings and `partial`, not a full rollback.

- [ ] **Step 7: Run tests**

Run:

```bash
go test ./internal/importer/raio -v
go test ./...
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add control-plane/fixtures/icao/raio.html control-plane/internal/importer/raio
git commit -m "feat(control-plane): import ICAO regional investigation mappings"
```

## Task 11: Implement invariant validation

**Files:**

- Create: `control-plane/internal/validation/validation.go`
- Create: `control-plane/internal/validation/validation_test.go`

- [ ] **Step 1: Write failing validation tests**

```go
func TestRunAcceptsSeededDatabase(t *testing.T) {
	db := testDB(t)
	report := Run(context.Background(), db, Options{})
	if report.HasErrors() {
		t.Fatalf("errors=%+v", report.Issues)
	}
}

func TestRunRejectsDirectJobsForExcludedCountries(t *testing.T) {
	db := testDB(t)
	af := countryID(t, db, "AF")
	source := insertSource(t, db)
	db.Exec(`INSERT INTO crawl_jobs(source_id,country_id,job_type,status,created_at)
		VALUES(?,?,'archive_crawl','pending',1)`, source, af)
	report := Run(context.Background(), db, Options{})
	if !report.Contains("excluded_direct_crawl") {
		t.Fatalf("issues=%+v", report.Issues)
	}
}

func TestRunFlagsPriorityDriftAndOpenConflicts(t *testing.T) {
	// Mutate PA priority_score and insert an open conflict.
	// Assert priority drift is error and open conflict is warning by default.
}
```

- [ ] **Step 2: Verify tests fail**

Run:

```bash
go test ./internal/validation -v
```

Expected: compile failure because `Run` is missing.

- [ ] **Step 3: Implement structured validation**

```go
type Severity string

const (
	Error   Severity = "error"
	Warning Severity = "warning"
)

type Issue struct {
	Code     string `json:"code"`
	Severity Severity `json:"severity"`
	Entity   string `json:"entity,omitempty"`
	Message  string `json:"message"`
}

type Report struct {
	Issues []Issue `json:"issues"`
}

type Options struct {
	ConflictsAreErrors bool
}
```

Implement every invariant listed in design section 12 as explicit SQL queries. Sort issues by severity, code, and entity for deterministic output.

- [ ] **Step 4: Run tests**

Run:

```bash
go test ./internal/validation -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add control-plane/internal/validation
git commit -m "feat(control-plane): validate coverage invariants"
```

## Task 12: Build deterministic atomic JSON export

**Files:**

- Create: `control-plane/internal/atomicfile/write.go`
- Create: `control-plane/internal/atomicfile/write_test.go`
- Create: `control-plane/internal/export/export.go`
- Create: `control-plane/internal/export/export_test.go`

- [ ] **Step 1: Write failing export tests**

```go
func TestBuildIsDeterministicAndOmitsRawSnapshots(t *testing.T) {
	db := testDB(t)
	at := time.Date(2026, 6, 22, 12, 0, 0, 0, time.UTC)
	first, err := Build(context.Background(), db, at)
	if err != nil {
		t.Fatal(err)
	}
	second, err := Build(context.Background(), db, at)
	if err != nil {
		t.Fatal(err)
	}
	a, _ := json.Marshal(first)
	b, _ := json.Marshal(second)
	if !bytes.Equal(a, b) {
		t.Fatal("export is not deterministic")
	}
	if bytes.Contains(a, []byte("raw_body")) || bytes.Contains(a, []byte("raw_contact")) {
		t.Fatal("export leaked raw operational data")
	}
	if len(first.Countries) != 249 {
		t.Fatalf("countries=%d", len(first.Countries))
	}
}

func TestWriteJSONAtomicallyReplacesDestination(t *testing.T) {
	// Seed destination with "old", call WriteJSON, decode complete JSON,
	// and assert no temporary file remains.
}
```

- [ ] **Step 2: Verify tests fail**

Run:

```bash
go test ./internal/export ./internal/atomicfile -v
```

Expected: compile failure because export functions are missing.

- [ ] **Step 3: Implement export types and ordered queries**

```go
type Document struct {
	SchemaVersion        int                    `json:"schema_version"`
	GeneratedAt          string                 `json:"generated_at"`
	Countries            []Country              `json:"countries"`
	Authorities          []Authority            `json:"authorities"`
	RegionalBodies       []RegionalBody          `json:"regional_bodies"`
	RegionalBodyMembers  []RegionalBodyMember    `json:"regional_body_members"`
	Sources              []Source                `json:"sources"`
	AircraftOriginRoutes []AircraftOriginRoute   `json:"aircraft_origin_routes"`
}
```

Every SQL query includes an explicit `ORDER BY`. Authority output includes field-level provenance labels (`curated_override`, `icao_snapshot`, or `seed`) but excludes raw contact blocks, snapshots, and private notes.

- [ ] **Step 4: Implement atomic writing**

Write to a temp file in the destination directory, `Sync`, close, `Chmod(0644)`, then `Rename`. Remove temp files on every error path.

- [ ] **Step 5: Run tests**

Run:

```bash
go test ./internal/export ./internal/atomicfile -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add control-plane/internal/export control-plane/internal/atomicfile
git commit -m "feat(control-plane): export deterministic coverage JSON"
```

## Task 13: Wire the CLI commands

**Files:**

- Create: `control-plane/internal/app/app.go`
- Create: `control-plane/internal/app/app_test.go`
- Create: `control-plane/cmd/aviation-coverage/main.go`

- [ ] **Step 1: Write failing CLI integration tests**

```go
func TestRunMigrateSeedValidateExport(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "coverage.db")
	outPath := filepath.Join(t.TempDir(), "coverage.json")
	var stdout, stderr bytes.Buffer

	for _, args := range [][]string{
		{"migrate", "--db", dbPath},
		{"seed", "--db", dbPath},
		{"validate", "--db", dbPath},
		{"export", "--db", dbPath, "--format", "json", "--output", outPath,
			"--generated-at", "2026-06-22T12:00:00Z"},
	} {
		stdout.Reset()
		stderr.Reset()
		if code := Run(context.Background(), args, &stdout, &stderr); code != 0 {
			t.Fatalf("args=%v code=%d stderr=%s", args, code, stderr.String())
		}
	}
	if _, err := os.Stat(outPath); err != nil {
		t.Fatal(err)
	}
}

func TestRunImportAIAFromFile(t *testing.T) {
	// Migrate + seed, invoke import-aia --source-file fixture,
	// and assert JSON result contains status and applied count.
}

func TestValidateReturnsNonZeroOnInvariantFailure(t *testing.T) {
	// Insert forbidden direct job, run validate, assert exit code 1.
}
```

- [ ] **Step 2: Verify tests fail**

Run:

```bash
go test ./internal/app -v
```

Expected: compile failure because `Run` is missing.

- [ ] **Step 3: Implement application command dispatch**

Use one `flag.FlagSet` per command with `ContinueOnError`. `Run` returns:

- `0` success;
- `1` command completed with validation/import failure;
- `2` CLI usage error.

For live import:

1. read `--source-file` when supplied;
2. otherwise call the bounded fetcher;
3. pass body and response metadata to the importer.

Require `--format json` for the foundation export. Support `--strict-conflicts` on validate.

- [ ] **Step 4: Add the thin binary entrypoint**

```go
package main

import (
	"context"
	"os"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/app"
)

func main() {
	os.Exit(app.Run(context.Background(), os.Args[1:], os.Stdout, os.Stderr))
}
```

- [ ] **Step 5: Run tests and build**

Run:

```bash
go test ./internal/app -v
go test ./...
go build ./cmd/aviation-coverage
```

Expected: PASS and binary build succeeds.

- [ ] **Step 6: Commit**

```bash
git add control-plane/internal/app control-plane/cmd
git commit -m "feat(control-plane): add aviation coverage CLI"
```

## Task 14: Add operator documentation and final verification

**Files:**

- Create: `control-plane/README.md`
- Modify: `README.md`

- [ ] **Step 1: Write operator documentation**

Document:

```bash
cd control-plane
go build -o aviation-coverage ./cmd/aviation-coverage
./aviation-coverage migrate --db coverage.db
./aviation-coverage seed --db coverage.db
./aviation-coverage import-aia --db coverage.db
./aviation-coverage import-raio --db coverage.db
./aviation-coverage validate --db coverage.db
./aviation-coverage export --db coverage.db --format json --output coverage.json
```

Also document:

- offline `--source-file` recovery/import;
- policy-excluded behavior;
- override precedence;
- meaning of `success`, `partial`, `failed`, and `unchanged`;
- live smoke commands are manual and network-dependent;
- harvested snapshots and generated DB/export files are not committed.

- [ ] **Step 2: Add repository catalogue entry**

Add `control-plane/` to the root layout and explain that it coordinates coverage metadata but does not replace independent source scrapers.

- [ ] **Step 3: Run formatting and static checks**

Run:

```bash
cd control-plane
go fmt ./...
go vet ./...
go test ./...
go build ./cmd/aviation-coverage
```

Expected: all commands exit 0.

- [ ] **Step 4: Run an offline end-to-end smoke**

Run:

```bash
tmpdir="$(mktemp -d)"
./aviation-coverage migrate --db "$tmpdir/coverage.db"
./aviation-coverage seed --db "$tmpdir/coverage.db"
./aviation-coverage import-aia --db "$tmpdir/coverage.db" --source-file fixtures/icao/aia.html
./aviation-coverage import-raio --db "$tmpdir/coverage.db" --source-file fixtures/icao/raio.html
./aviation-coverage validate --db "$tmpdir/coverage.db"
./aviation-coverage export --db "$tmpdir/coverage.db" --format json --output "$tmpdir/coverage.json" --generated-at 2026-06-22T12:00:00Z
go test ./...
```

Expected:

- every command exits 0 except a fixture import may exit 1 only if its intentionally malformed record is configured as an import failure rather than `partial`;
- `validate` exits 0;
- exported JSON contains 249 countries;
- repeated imports return `unchanged`;
- no source DB, snapshots, or generated export are added to Git.

- [ ] **Step 5: Inspect repository state**

Run:

```bash
git status --short
git diff --check
```

Expected: only intended source, fixture, seed, and documentation changes; no generated binaries or databases.

- [ ] **Step 6: Commit**

```bash
git add control-plane/README.md README.md
git commit -m "docs(control-plane): document coverage operations"
```

## Final acceptance checklist

- [ ] `go test ./...` passes from `control-plane/`.
- [ ] `go vet ./...` passes.
- [ ] `go build ./cmd/aviation-coverage` passes.
- [ ] Fresh migration creates all 19 canonical/provenance tables.
- [ ] Seed creates exactly 249 ISO countries.
- [ ] Required ECCAA, BAGAIA, and IAC mappings validate.
- [ ] AIA importer preserves raw source data, stages every record, and protects overrides.
- [ ] RAIO importer distinguishes RAIO from ICM and preserves curated mappings.
- [ ] Identical imports return `unchanged`.
- [ ] Policy-excluded countries cannot receive direct crawl jobs.
- [ ] JSON export is deterministic and excludes raw snapshots/private notes.
- [ ] Existing source packages remain unchanged and their tests are not coupled to the control plane.

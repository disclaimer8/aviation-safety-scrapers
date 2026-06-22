package validation_test

import (
	"context"
	"database/sql"
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/database"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/migrations"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/seed"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/validation"
)

// testDB creates a migrated + seeded database for testing.
func testDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := database.Open(t.TempDir() + "/coverage.db")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := db.Close(); err != nil {
			t.Errorf("close database: %v", err)
		}
	})
	if err := migrations.Apply(context.Background(), db); err != nil {
		t.Fatal(err)
	}
	if _, err := seed.Apply(context.Background(), db); err != nil {
		t.Fatal(err)
	}
	return db
}

// countryID looks up the id for a country by iso2 code.
func countryID(t *testing.T, db *sql.DB, iso2 string) int64 {
	t.Helper()
	var id int64
	if err := db.QueryRow(`SELECT id FROM countries WHERE iso2 = ?`, iso2).Scan(&id); err != nil {
		t.Fatalf("lookup country %s: %v", iso2, err)
	}
	return id
}

// insertSource inserts a minimal source row and returns its id.
func insertSource(t *testing.T, db *sql.DB) int64 {
	t.Helper()
	result, err := db.Exec(`
		INSERT INTO sources (name, url, canonical_url, source_type, source_tier)
		VALUES ('Test source', 'https://test-validation.example', 'https://test-validation.example',
			'official_aai', 1)
	`)
	if err != nil {
		t.Fatalf("insert source: %v", err)
	}
	id, err := result.LastInsertId()
	if err != nil {
		t.Fatal(err)
	}
	return id
}

// TestRunAcceptsSeededDatabase verifies that a freshly migrated+seeded database
// produces zero errors.
func TestRunAcceptsSeededDatabase(t *testing.T) {
	db := testDB(t)
	report := validation.Run(context.Background(), db, validation.Options{})
	if report.HasErrors() {
		t.Fatalf("seeded database has errors: %+v", report.Issues)
	}
}

// TestRunRejectsDirectJobsForExcludedCountries verifies that inserting an
// archive_crawl or pdf_discovery crawl job for a policy_excluded country
// produces an excluded_direct_crawl error.
func TestRunRejectsDirectJobsForExcludedCountries(t *testing.T) {
	db := testDB(t)
	af := countryID(t, db, "AF")
	source := insertSource(t, db)
	if _, err := db.Exec(`INSERT INTO crawl_jobs(source_id,country_id,job_type,status,created_at)
		VALUES(?,?,'archive_crawl','pending',1)`, source, af); err != nil {
		t.Fatalf("insert crawl_job: %v", err)
	}
	report := validation.Run(context.Background(), db, validation.Options{})
	if !report.Contains("excluded_direct_crawl") {
		t.Fatalf("expected excluded_direct_crawl issue, got: %+v", report.Issues)
	}
}

// TestRunFlagsPriorityDriftAndOpenConflicts verifies that a mutated
// priority_score produces a priority_drift error, and open conflicts are
// flagged as a warning by default and error when ConflictsAreErrors=true.
func TestRunFlagsPriorityDriftAndOpenConflicts(t *testing.T) {
	db := testDB(t)

	// Mutate PA's priority_score to an incorrect value.
	if _, err := db.Exec(`UPDATE countries SET priority_score = 99999.0 WHERE iso2 = 'PA'`); err != nil {
		t.Fatalf("mutate PA priority_score: %v", err)
	}

	// Insert an open import conflict. We need a source, a snapshot, an import_run,
	// and a staged_authority first.
	source := insertSource(t, db)

	snapshotRes, err := db.Exec(`
		INSERT INTO source_snapshots (source_id, source_url, fetched_at, checksum, size_bytes)
		VALUES (?, 'https://test-validation.example/snap', 1000, 'test-ck-validation', 1)
	`, source)
	if err != nil {
		t.Fatalf("insert snapshot: %v", err)
	}
	snapID, _ := snapshotRes.LastInsertId()

	runRes, err := db.Exec(`
		INSERT INTO import_runs (importer, source_url, source_snapshot_id, started_at, status)
		VALUES ('test', 'https://test-validation.example/snap', ?, 1000, 'success')
	`, snapID)
	if err != nil {
		t.Fatalf("insert import_run: %v", err)
	}
	runID, _ := runRes.LastInsertId()

	// Insert a staged authority so we can reference it in the conflict.
	stagedRes, err := db.Exec(`
		INSERT INTO staged_authorities (import_run_id, country_label, authority_name, record_checksum)
		VALUES (?, 'Panama', 'AAC Panama', 'staged-ck-validation-1')
	`, runID)
	if err != nil {
		t.Fatalf("insert staged_authority: %v", err)
	}
	stagedID, _ := stagedRes.LastInsertId()

	paID := countryID(t, db, "PA")
	if _, err := db.Exec(`
		INSERT INTO import_conflicts (
			import_run_id, staged_authority_id, target_entity_type, target_entity_id,
			field_name, current_value, incoming_value, reason, review_status
		) VALUES (?, ?, 'authority', ?, 'name', 'old', 'new', 'test conflict', 'open')
	`, runID, stagedID, paID); err != nil {
		t.Fatalf("insert import_conflict: %v", err)
	}

	// By default: priority drift = Error, open conflict = Warning.
	report := validation.Run(context.Background(), db, validation.Options{})
	if !report.Contains("priority_drift") {
		t.Fatalf("expected priority_drift issue, got: %+v", report.Issues)
	}
	hasPriorityError := false
	hasConflictWarning := false
	for _, iss := range report.Issues {
		if iss.Code == "priority_drift" && iss.Severity == validation.Error {
			hasPriorityError = true
		}
		if iss.Code == "open_import_conflicts" && iss.Severity == validation.Warning {
			hasConflictWarning = true
		}
	}
	if !hasPriorityError {
		t.Fatalf("priority_drift should be Error, got issues: %+v", report.Issues)
	}
	if !hasConflictWarning {
		t.Fatalf("open_import_conflicts should be Warning by default, got issues: %+v", report.Issues)
	}

	// With ConflictsAreErrors=true, the open conflict should become an Error.
	reportStrict := validation.Run(context.Background(), db, validation.Options{ConflictsAreErrors: true})
	hasConflictError := false
	for _, iss := range reportStrict.Issues {
		if iss.Code == "open_import_conflicts" && iss.Severity == validation.Error {
			hasConflictError = true
		}
	}
	if !hasConflictError {
		t.Fatalf("open_import_conflicts should be Error when ConflictsAreErrors=true, got issues: %+v", reportStrict.Issues)
	}
}

// TestRunFlagsMissingRequiredBody verifies that when a required regional body
// (e.g. ECCAA) is absent from the database, the report contains
// "regional_body_missing" and HasErrors() returns true.
func TestRunFlagsMissingRequiredBody(t *testing.T) {
	db := testDB(t)

	// Delete ECCAA members first (FK), then the body itself.
	if _, err := db.Exec(`
		DELETE FROM regional_body_members
		WHERE regional_body_id = (SELECT id FROM regional_bodies WHERE code = 'ECCAA')
	`); err != nil {
		t.Fatalf("delete ECCAA members: %v", err)
	}
	if _, err := db.Exec(`DELETE FROM regional_bodies WHERE code = 'ECCAA'`); err != nil {
		t.Fatalf("delete ECCAA body: %v", err)
	}

	report := validation.Run(context.Background(), db, validation.Options{})
	if !report.Contains("regional_body_missing") {
		t.Fatalf("expected regional_body_missing issue, got: %+v", report.Issues)
	}
	if !report.HasErrors() {
		t.Fatalf("expected HasErrors()=true, got issues: %+v", report.Issues)
	}
}

// TestRunFlagsMissingAuthorityProvenance verifies that an authority with a
// non-empty website_url but no matching authority_field_provenance row is
// flagged with "authority_provenance_missing".
func TestRunFlagsMissingAuthorityProvenance(t *testing.T) {
	db := testDB(t)

	// Insert a country + authority directly (bypassing ApplyAuthority).
	// Use a country already in the seed (e.g. DE).
	deID := countryID(t, db, "DE")
	if _, err := db.Exec(`
		INSERT INTO authorities (country_id, name, normalized_name, type, website_url,
			source_url, source_name)
		VALUES (?, 'Test CAA', 'test caa', 'caa', 'https://testcaa.example',
			'https://testcaa.example/source', 'Test Source')
	`, deID); err != nil {
		t.Fatalf("insert authority: %v", err)
	}

	report := validation.Run(context.Background(), db, validation.Options{})
	if !report.Contains("authority_provenance_missing") {
		t.Fatalf("expected authority_provenance_missing issue, got: %+v", report.Issues)
	}
}

// TestRunFlagsSourceTierTypeMismatch verifies that a source with a tier/type
// mismatch is flagged.
func TestRunFlagsSourceTierTypeMismatch(t *testing.T) {
	db := testDB(t)

	// Insert a source with tier=1 (should only allow official_aai) but type=media.
	// We bypass the seed validation by inserting directly.
	if _, err := db.Exec(`
		INSERT INTO sources (name, url, canonical_url, source_type, source_tier)
		VALUES ('Bad Source', 'https://bad-tier.example', 'https://bad-tier.example', 'media', 1)
	`); err != nil {
		t.Fatalf("insert bad source: %v", err)
	}

	report := validation.Run(context.Background(), db, validation.Options{})
	if !report.Contains("source_tier_type_mismatch") {
		t.Fatalf("expected source_tier_type_mismatch issue, got: %+v", report.Issues)
	}
}

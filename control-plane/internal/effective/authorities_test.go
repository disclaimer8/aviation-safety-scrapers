package effective

import (
	"context"
	"database/sql"
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/database"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/migrations"
)

// testDB opens a fresh in-process SQLite database with all migrations applied.
func testDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := database.Open(t.TempDir() + "/coverage.db")
	if err != nil {
		t.Fatal(err)
	}
	if err := migrations.Apply(context.Background(), db); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := db.Close(); err != nil {
			t.Errorf("close database: %v", err)
		}
	})
	return db
}

// countryID inserts a minimal countries row for iso2 and returns its ID.
func countryID(t *testing.T, db *sql.DB, iso2 string) int64 {
	t.Helper()
	result, err := db.Exec(`
		INSERT INTO countries (
			iso2, iso3, name, region, policy_status, coverage_status,
			coverage_score, effort_score
		) VALUES (?, ?, ?, 'AFI', 'allowed', 'unknown', 0, 1)
	`, iso2, iso2+"X", "Country "+iso2)
	if err != nil {
		t.Fatal(err)
	}
	id, err := result.LastInsertId()
	if err != nil {
		t.Fatal(err)
	}
	return id
}

// insertAuthority inserts a national_aai authority with the given website and
// returns its ID.
func insertAuthority(t *testing.T, db *sql.DB, countryID int64, name, website string) int64 {
	t.Helper()
	result, err := db.Exec(`
		INSERT INTO authorities (
			country_id, normalized_name, name, type, website_url,
			source_url, source_name
		) VALUES (?, ?, ?, 'national_aai', ?, 'https://seed.example', 'seed')
	`, countryID, normalize(name), name, nullable(website))
	if err != nil {
		t.Fatal(err)
	}
	id, err := result.LastInsertId()
	if err != nil {
		t.Fatal(err)
	}
	return id
}

// insertAuthorityField sets an arbitrary text column on an existing authority.
func insertAuthorityField(t *testing.T, db *sql.DB, authorityID int64, field, value string) {
	t.Helper()
	// field names here are test-controlled constants, never user input.
	if _, err := db.Exec(
		`UPDATE authorities SET `+field+` = ? WHERE id = ?`, value, authorityID,
	); err != nil {
		t.Fatal(err)
	}
}

// insertOverride inserts an active field_overrides row.
func insertOverride(t *testing.T, db *sql.DB, entityType string, entityID int64, field, value string) int64 {
	t.Helper()
	result, err := db.Exec(`
		INSERT INTO field_overrides (
			entity_type, entity_id, field_name, value, value_type, reason, author, active
		) VALUES (?, ?, ?, ?, 'text', 'curated', 'tester', 1)
	`, entityType, entityID, field, value)
	if err != nil {
		t.Fatal(err)
	}
	id, err := result.LastInsertId()
	if err != nil {
		t.Fatal(err)
	}
	return id
}

// insertRun inserts an import_runs row with an explicit ID so that conflicts can
// reference it (import_conflicts.import_run_id is NOT NULL + FK).
func insertRun(t *testing.T, db *sql.DB, id int64) {
	t.Helper()
	if _, err := db.Exec(`
		INSERT INTO import_runs (id, importer, source_url, started_at, status)
		VALUES (?, 'icao', 'https://icao.example', 0, 'running')
	`, id); err != nil {
		t.Fatal(err)
	}
}

// insertSnapshot inserts a source_snapshots row with an explicit ID so that
// provenance can reference it (authority_field_provenance.snapshot_id FK).
func insertSnapshot(t *testing.T, db *sql.DB, id int64) {
	t.Helper()
	res, err := db.Exec(`
		INSERT INTO sources (name, url, canonical_url, source_type, source_tier)
		VALUES ('ICAO', 'https://icao.example', 'https://icao.example', 'icao_elibrary', 1)
	`)
	if err != nil {
		t.Fatal(err)
	}
	sourceID, err := res.LastInsertId()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`
		INSERT INTO source_snapshots (
			id, source_id, source_url, fetched_at, checksum, size_bytes
		) VALUES (?, ?, 'https://icao.example', 0, ?, 0)
	`, id, sourceID, "checksum-"+string(rune('a'+id))); err != nil {
		t.Fatal(err)
	}
}

// readField reads one text column from an authority, returning "" for NULL.
func readField(t *testing.T, db *sql.DB, authorityID int64, field string) string {
	t.Helper()
	var v sql.NullString
	if err := db.QueryRow(
		`SELECT `+field+` FROM authorities WHERE id = ?`, authorityID,
	).Scan(&v); err != nil {
		t.Fatal(err)
	}
	return v.String
}

// countConflicts returns the number of open conflicts for a field.
func countConflicts(t *testing.T, db *sql.DB, authorityID int64, field string) int {
	t.Helper()
	var n int
	if err := db.QueryRow(`
		SELECT count(*) FROM import_conflicts
		WHERE target_entity_type = 'authority' AND target_entity_id = ? AND field_name = ?
	`, authorityID, field).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}

// readProvenance returns the provenance kind and the override/snapshot ids for a
// (authority, field) pair. Missing row yields kind="".
func readProvenance(t *testing.T, db *sql.DB, authorityID int64, field string) (kind string, snapshotID, overrideID sql.NullInt64) {
	t.Helper()
	err := db.QueryRow(`
		SELECT provenance_kind, snapshot_id, override_id
		FROM authority_field_provenance
		WHERE authority_id = ? AND field_name = ?
	`, authorityID, field).Scan(&kind, &snapshotID, &overrideID)
	if err == sql.ErrNoRows {
		return "", sql.NullInt64{}, sql.NullInt64{}
	}
	if err != nil {
		t.Fatal(err)
	}
	return kind, snapshotID, overrideID
}

func TestApplyAuthorityPreservesOverrideAndCreatesConflict(t *testing.T) {
	db := testDB(t)
	cid := countryID(t, db, "SN")
	authorityID := insertAuthority(t, db, cid, "BEA Senegal", "https://old.example")
	overrideID := insertOverride(t, db, "authority", authorityID, "website_url", "https://curated.example")
	insertRun(t, db, 7)
	insertSnapshot(t, db, 9)

	result, err := ApplyAuthority(context.Background(), db, IncomingAuthority{
		RunID: 7, CountryID: cid, Name: "BEA Senegal",
		NormalizedName: "bea senegal", Type: "national_aai",
		WebsiteURL: "https://incoming.example", SourceURL: "https://icao.example",
		SourceName: "ICAO AIA", SnapshotID: 9,
	})
	if err != nil {
		t.Fatal(err)
	}
	if result.Conflicts != 1 {
		t.Fatalf("conflicts=%d, want 1", result.Conflicts)
	}
	if got := readField(t, db, authorityID, "website_url"); got != "https://curated.example" {
		t.Fatalf("website=%q, want curated value (override must win)", got)
	}

	// Provenance must be curated_override with the matching override_id.
	kind, snap, ovr := readProvenance(t, db, authorityID, "website_url")
	if kind != "curated_override" || !ovr.Valid || ovr.Int64 != overrideID || snap.Valid {
		t.Fatalf("provenance kind=%q snap=%v override=%v, want curated_override with override_id=%d",
			kind, snap, ovr, overrideID)
	}
}

func TestMissingIncomingValueDoesNotEraseKnownValue(t *testing.T) {
	db := testDB(t)
	cid := countryID(t, db, "KE")
	authorityID := insertAuthority(t, db, cid, "AIB Kenya", "https://aib.example")
	insertAuthorityField(t, db, authorityID, "contact_email", "known@aib.example")
	insertRun(t, db, 11)
	insertSnapshot(t, db, 12)

	result, err := ApplyAuthority(context.Background(), db, IncomingAuthority{
		RunID: 11, CountryID: cid, Name: "AIB Kenya",
		NormalizedName: "aib kenya", Type: "national_aai",
		WebsiteURL:   "https://aib.example", // unchanged
		ContactEmail: "",                    // upstream removed it
		SourceURL:    "https://icao.example", SourceName: "ICAO AIA", SnapshotID: 12,
	})
	if err != nil {
		t.Fatal(err)
	}
	if got := readField(t, db, authorityID, "contact_email"); got != "known@aib.example" {
		t.Fatalf("contact_email=%q, want preserved known value", got)
	}
	if n := countConflicts(t, db, authorityID, "contact_email"); n != 1 {
		t.Fatalf("contact_email conflicts=%d, want 1 removal conflict", n)
	}
	if result.Conflicts < 1 {
		t.Fatalf("result.Conflicts=%d, want >=1", result.Conflicts)
	}

	// A removal conflict must carry the upstream_removal reason.
	var reason string
	if err := db.QueryRow(`
		SELECT reason FROM import_conflicts
		WHERE target_entity_id = ? AND field_name = 'contact_email'
	`, authorityID).Scan(&reason); err != nil {
		t.Fatal(err)
	}
	if reason != "upstream_removal" {
		t.Fatalf("reason=%q, want upstream_removal", reason)
	}
}

func TestCleanApplyRecordsSnapshotProvenance(t *testing.T) {
	db := testDB(t)
	cid := countryID(t, db, "GH")
	authorityID := insertAuthority(t, db, cid, "AIB Ghana", "")
	insertRun(t, db, 21)
	insertSnapshot(t, db, 22)

	result, err := ApplyAuthority(context.Background(), db, IncomingAuthority{
		RunID: 21, CountryID: cid, Name: "AIB Ghana",
		NormalizedName: "aib ghana", Type: "national_aai",
		WebsiteURL: "https://aibghana.example",
		SourceURL:  "https://icao.example", SourceName: "ICAO AIA", SnapshotID: 22,
	})
	if err != nil {
		t.Fatal(err)
	}
	if got := readField(t, db, authorityID, "website_url"); got != "https://aibghana.example" {
		t.Fatalf("website=%q, want incoming value applied", got)
	}
	if result.Conflicts != 0 {
		t.Fatalf("conflicts=%d, want 0 for clean apply", result.Conflicts)
	}
	if result.Applied < 1 {
		t.Fatalf("applied=%d, want >=1", result.Applied)
	}
	kind, snap, ovr := readProvenance(t, db, authorityID, "website_url")
	if kind != "icao_snapshot" || !snap.Valid || snap.Int64 != 22 || ovr.Valid {
		t.Fatalf("provenance kind=%q snap=%v ovr=%v, want icao_snapshot snapshot_id=22", kind, snap, ovr)
	}
}

func TestConflictInsertionIsIdempotent(t *testing.T) {
	db := testDB(t)
	cid := countryID(t, db, "NG")
	authorityID := insertAuthority(t, db, cid, "AIB Nigeria", "https://old.example")
	insertOverride(t, db, "authority", authorityID, "website_url", "https://curated.example")
	insertRun(t, db, 31)
	insertSnapshot(t, db, 32)

	in := IncomingAuthority{
		RunID: 31, CountryID: cid, Name: "AIB Nigeria",
		NormalizedName: "aib nigeria", Type: "national_aai",
		WebsiteURL: "https://incoming.example",
		SourceURL:  "https://icao.example", SourceName: "ICAO AIA", SnapshotID: 32,
	}

	if _, err := ApplyAuthority(context.Background(), db, in); err != nil {
		t.Fatal(err)
	}
	second, err := ApplyAuthority(context.Background(), db, in)
	if err != nil {
		t.Fatal(err)
	}
	if second.Conflicts != 0 {
		t.Fatalf("second call conflicts=%d, want 0 (idempotent)", second.Conflicts)
	}
	if n := countConflicts(t, db, authorityID, "website_url"); n != 1 {
		t.Fatalf("total website_url conflicts=%d, want 1", n)
	}
}

func TestProvenanceHasOneRowPerField(t *testing.T) {
	db := testDB(t)
	cid := countryID(t, db, "ZA")
	authorityID := insertAuthority(t, db, cid, "SACAA", "")
	insertRun(t, db, 41)
	insertSnapshot(t, db, 42)

	in := IncomingAuthority{
		RunID: 41, CountryID: cid, Name: "SACAA",
		NormalizedName: "sacaa", Type: "national_aai",
		WebsiteURL:   "https://sacaa.example",
		ArchiveURL:   "https://sacaa.example/archive",
		ContactEmail: "info@sacaa.example",
		ContactPhone: "+27 11 000 0000",
		SourceURL:    "https://icao.example", SourceName: "ICAO AIA", SnapshotID: 42,
	}
	// Apply twice; provenance must stay exactly one row per field.
	if _, err := ApplyAuthority(context.Background(), db, in); err != nil {
		t.Fatal(err)
	}
	if _, err := ApplyAuthority(context.Background(), db, in); err != nil {
		t.Fatal(err)
	}

	var rows int
	if err := db.QueryRow(`
		SELECT count(*) FROM authority_field_provenance WHERE authority_id = ?
	`, authorityID).Scan(&rows); err != nil {
		t.Fatal(err)
	}
	if rows != 4 {
		t.Fatalf("provenance rows=%d, want 4 (one per written field)", rows)
	}
}

func TestApplyAuthorityUpsertsWhenMissing(t *testing.T) {
	db := testDB(t)
	cid := countryID(t, db, "TZ")
	insertRun(t, db, 51)
	insertSnapshot(t, db, 52)

	result, err := ApplyAuthority(context.Background(), db, IncomingAuthority{
		RunID: 51, CountryID: cid, Name: "TCAA",
		NormalizedName: "tcaa", Type: "national_aai",
		WebsiteURL: "https://tcaa.example",
		SourceURL:  "https://icao.example", SourceName: "ICAO AIA", SnapshotID: 52,
	})
	if err != nil {
		t.Fatal(err)
	}
	if result.AuthorityID == 0 {
		t.Fatal("expected a created authority ID")
	}
	if got := readField(t, db, result.AuthorityID, "website_url"); got != "https://tcaa.example" {
		t.Fatalf("website=%q, want incoming applied to new authority", got)
	}
}

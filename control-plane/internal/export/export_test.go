package export_test

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/database"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/export"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/migrations"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/seed"
)

func testDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := database.Open(filepath.Join(t.TempDir(), "coverage.db"))
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

func seededDB(t *testing.T) *sql.DB {
	t.Helper()
	db := testDB(t)
	if _, err := seed.Apply(context.Background(), db); err != nil {
		t.Fatal(err)
	}
	return db
}

// TestBuildIsDeterministicAndOmitsRawSnapshots verifies spec §13 determinism
// and leakage-prevention requirements.
func TestBuildIsDeterministicAndOmitsRawSnapshots(t *testing.T) {
	db := seededDB(t)
	at := time.Date(2026, 6, 22, 12, 0, 0, 0, time.UTC)

	first, err := export.Build(context.Background(), db, at)
	if err != nil {
		t.Fatal(err)
	}
	second, err := export.Build(context.Background(), db, at)
	if err != nil {
		t.Fatal(err)
	}

	a, err := json.Marshal(first)
	if err != nil {
		t.Fatal(err)
	}
	b, err := json.Marshal(second)
	if err != nil {
		t.Fatal(err)
	}

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

// TestDocumentShape verifies schema_version, generated_at format, and that
// all top-level slices are non-nil (empty slice, not null).
func TestDocumentShape(t *testing.T) {
	db := seededDB(t)
	at := time.Date(2026, 6, 22, 0, 0, 0, 0, time.UTC)

	doc, err := export.Build(context.Background(), db, at)
	if err != nil {
		t.Fatal(err)
	}

	if doc.SchemaVersion != 1 {
		t.Errorf("schema_version=%d want 1", doc.SchemaVersion)
	}
	if doc.GeneratedAt != "2026-06-22T00:00:00Z" {
		t.Errorf("generated_at=%q", doc.GeneratedAt)
	}
	if doc.Countries == nil {
		t.Error("countries is nil")
	}
	if doc.Authorities == nil {
		t.Error("authorities is nil")
	}
	if doc.RegionalBodies == nil {
		t.Error("regional_bodies is nil")
	}
	if doc.RegionalBodyMembers == nil {
		t.Error("regional_body_members is nil")
	}
	if doc.Sources == nil {
		t.Error("sources is nil")
	}
	if doc.AircraftOriginRoutes == nil {
		t.Error("aircraft_origin_routes is nil")
	}
}

// TestAuthorityProvenanceExport inserts an authority and authority_field_provenance
// rows, then verifies the exported Authority carries the correct per-field
// provenance_kind labels and does NOT leak snapshot/override IDs, raw values,
// or notes.
//
// We use only 'seed' and 'curated_override' provenance kinds to keep the test
// self-contained (icao_snapshot requires a source_snapshots row with a
// non-null snapshot_id per the schema CHECK constraint).
func TestAuthorityProvenanceExport(t *testing.T) {
	db := seededDB(t)
	ctx := context.Background()

	// Find a country ID for a known seeded country (US).
	var countryID int64
	if err := db.QueryRowContext(ctx, `SELECT id FROM countries WHERE iso2='US'`).Scan(&countryID); err != nil {
		t.Fatalf("lookup US country: %v", err)
	}

	// Insert a test authority.
	var authorityID int64
	err := db.QueryRowContext(ctx, `
		INSERT INTO authorities (
			country_id, normalized_name, name, type,
			website_url, archive_url, contact_email, contact_phone,
			source_url, source_name, status
		) VALUES (?, 'test authority', 'Test Authority', 'national_aai',
			'https://example.com', NULL, 'test@example.com', NULL,
			'https://source.example.com', 'TestSource', 'ok')
		RETURNING id
	`, countryID).Scan(&authorityID)
	if err != nil {
		t.Fatalf("insert authority: %v", err)
	}

	// Insert a field_override for the curated_override provenance kind.
	var overrideID int64
	err = db.QueryRowContext(ctx, `
		INSERT INTO field_overrides (
			entity_type, entity_id, field_name, value, value_type, reason, author
		) VALUES ('authority', ?, 'contact_email', 'test@example.com', 'text',
			'manual curation', 'test')
		RETURNING id
	`, authorityID).Scan(&overrideID)
	if err != nil {
		t.Fatalf("insert field_override: %v", err)
	}

	// Insert field-level provenance: website_url from seed, contact_email from curated override.
	_, err = db.ExecContext(ctx, `
		INSERT INTO authority_field_provenance
			(authority_id, field_name, effective_value, provenance_kind, snapshot_id, override_id)
		VALUES
			(?, 'website_url',   'https://example.com', 'seed',             NULL, NULL),
			(?, 'contact_email', 'test@example.com',    'curated_override', NULL, ?)
	`, authorityID, authorityID, overrideID)
	if err != nil {
		t.Fatalf("insert provenance rows: %v", err)
	}

	at := time.Date(2026, 6, 22, 12, 0, 0, 0, time.UTC)
	doc, err := export.Build(ctx, db, at)
	if err != nil {
		t.Fatal(err)
	}

	// Find the exported authority.
	var found *export.Authority
	for i := range doc.Authorities {
		if doc.Authorities[i].NormalizedName == "test authority" {
			a := doc.Authorities[i]
			found = &a
			break
		}
	}
	if found == nil {
		t.Fatal("exported authority not found")
	}

	// Verify provenance labels.
	if found.Provenance == nil {
		t.Fatal("provenance map is nil")
	}
	if found.Provenance["website_url"] != "seed" {
		t.Errorf("website_url provenance=%q want seed", found.Provenance["website_url"])
	}
	if found.Provenance["contact_email"] != "curated_override" {
		t.Errorf("contact_email provenance=%q want curated_override", found.Provenance["contact_email"])
	}
	// Fields with no provenance row must not appear.
	if _, ok := found.Provenance["archive_url"]; ok {
		t.Error("archive_url unexpectedly present in provenance")
	}
	if _, ok := found.Provenance["contact_phone"]; ok {
		t.Error("contact_phone unexpectedly present in provenance")
	}

	// Marshal and verify no raw/private leakage.
	data, _ := json.Marshal(doc)
	if bytes.Contains(data, []byte("raw_body")) || bytes.Contains(data, []byte("raw_contact")) {
		t.Error("export leaked raw operational data")
	}
	// snapshot_id and override_id must not appear as JSON keys in the export.
	if bytes.Contains(data, []byte(`"snapshot_id"`)) {
		t.Error("export leaked snapshot_id")
	}
	if bytes.Contains(data, []byte(`"override_id"`)) {
		t.Error("export leaked override_id")
	}
}

// TestWriteJSONAtomicallyReplacesDestination seeds a destination file with
// "old", calls WriteJSON, decodes the resulting file as a full Document, and
// verifies no temporary file remains in the output directory.
func TestWriteJSONAtomicallyReplacesDestination(t *testing.T) {
	db := seededDB(t)
	dir := t.TempDir()
	dst := filepath.Join(dir, "export.json")

	// Seed destination with old content.
	if err := os.WriteFile(dst, []byte(`"old"`), 0644); err != nil {
		t.Fatal(err)
	}

	at := time.Date(2026, 6, 22, 12, 0, 0, 0, time.UTC)
	if err := export.WriteJSON(context.Background(), db, dst, at); err != nil {
		t.Fatalf("WriteJSON: %v", err)
	}

	// Decode and verify the output is a valid Document.
	raw, err := os.ReadFile(dst)
	if err != nil {
		t.Fatalf("read output: %v", err)
	}
	var doc export.Document
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("unmarshal output: %v", err)
	}
	if doc.SchemaVersion != 1 {
		t.Errorf("schema_version=%d", doc.SchemaVersion)
	}
	if len(doc.Countries) != 249 {
		t.Errorf("countries=%d", len(doc.Countries))
	}

	// Assert no temp file remains in the output directory.
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		if e.Name() != "export.json" {
			t.Errorf("unexpected file after WriteJSON: %s", e.Name())
		}
	}
}

// TestCountriesOrderedByISO2 verifies the exported countries list is sorted.
func TestCountriesOrderedByISO2(t *testing.T) {
	db := seededDB(t)
	doc, err := export.Build(context.Background(), db, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	for i := 1; i < len(doc.Countries); i++ {
		if doc.Countries[i].ISO2 < doc.Countries[i-1].ISO2 {
			t.Fatalf("countries not sorted at index %d: %s < %s",
				i, doc.Countries[i].ISO2, doc.Countries[i-1].ISO2)
		}
	}
}

// TestDocumentOmitsPrivateNotes verifies that none of the string fields in the
// exported JSON encode private operational data labels ("notes").
// (The DB seed stores notes in some countries; they must not appear in export.)
func TestDocumentOmitsPrivateNotes(t *testing.T) {
	db := seededDB(t)
	doc, err := export.Build(context.Background(), db, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	data, _ := json.Marshal(doc)
	// "notes" as a JSON key must not appear in the export document.
	// We check for `"notes":` (with colon) to avoid false positives inside values.
	if bytes.Contains(data, []byte(`"notes":`)) {
		t.Error(`export contains "notes" key — private operational data leaked`)
	}
}

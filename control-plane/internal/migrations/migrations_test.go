package migrations

import (
	"context"
	"database/sql"
	"testing"
	"testing/fstest"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/database"
)

func openTestDB(t *testing.T) *sql.DB {
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
	return db
}

func applyTestSchema(t *testing.T) *sql.DB {
	t.Helper()

	db := openTestDB(t)
	if err := Apply(context.Background(), db); err != nil {
		t.Fatal(err)
	}
	return db
}

func TestApplyCreatesCompleteSchemaAndIsIdempotent(t *testing.T) {
	db := openTestDB(t)
	ctx := context.Background()

	if err := Apply(ctx, db); err != nil {
		t.Fatal(err)
	}
	if err := Apply(ctx, db); err != nil {
		t.Fatalf("second apply: %v", err)
	}

	required := []string{
		"schema_migrations",
		"countries",
		"authorities",
		"regional_bodies",
		"regional_body_members",
		"sources",
		"aircraft_origin_routes",
		"events",
		"reports",
		"event_source_links",
		"investigation_participants",
		"crawl_jobs",
		"crawl_errors",
		"import_runs",
		"source_snapshots",
		"staged_authorities",
		"staged_regional_bodies",
		"staged_wayback_documents",
		"staged_regional_documents",
		"staged_manufacturer_documents",
		"field_overrides",
		"import_conflicts",
		"authority_requests",
	}
	for _, table := range required {
		var got string
		err := db.QueryRowContext(ctx,
			`SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?`,
			table,
		).Scan(&got)
		if err != nil {
			t.Fatalf("find table %s: %v", table, err)
		}
		if got != table {
			t.Fatalf("table=%q, want %q", got, table)
		}
	}

	rows, err := db.QueryContext(ctx, `
		SELECT version, name
		FROM schema_migrations
		ORDER BY version
	`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()

	var migrations []string
	for rows.Next() {
		var version int
		var name string
		if err := rows.Scan(&version, &name); err != nil {
			t.Fatal(err)
		}
		migrations = append(migrations, name)
		if version != len(migrations) {
			t.Fatalf("migration version=%d at position %d", version, len(migrations))
		}
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	if len(migrations) != 12 {
		t.Fatalf("migration rows=%d, want 12 (%v)", len(migrations), migrations)
	}
}

func TestSchemaRejectsInvalidCountryEnumsAndScores(t *testing.T) {
	db := applyTestSchema(t)

	tests := []struct {
		name            string
		policy          string
		coverage        string
		coverageScore   int
		effort          int
		expected        int
		expectedQuality int
		countryGroup    any
	}{
		{
			name: "policy status", policy: "bad", coverage: "unknown",
			coverageScore: 0, effort: 1, expected: 0, expectedQuality: 1,
		},
		{
			name: "coverage status", policy: "allowed", coverage: "bad",
			coverageScore: 0, effort: 1, expected: 0, expectedQuality: 1,
		},
		{
			name: "coverage score", policy: "allowed", coverage: "unknown",
			coverageScore: 6, effort: 1, expected: 0, expectedQuality: 1,
		},
		{
			name: "effort score", policy: "allowed", coverage: "unknown",
			coverageScore: 0, effort: 0, expected: 0, expectedQuality: 1,
		},
		{
			name: "expected records", policy: "allowed", coverage: "unknown",
			coverageScore: 0, effort: 1, expected: -1, expectedQuality: 1,
		},
		{
			name: "expected source quality", policy: "allowed", coverage: "unknown",
			coverageScore: 0, effort: 1, expected: 0, expectedQuality: 6,
		},
		{
			name: "country group", policy: "allowed", coverage: "unknown",
			coverageScore: 0, effort: 1, expected: 0, expectedQuality: 1,
			countryGroup: "E",
		},
	}

	for i, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			_, err := db.Exec(`
				INSERT INTO countries (
					iso2, iso3, name, region, policy_status, coverage_status,
					coverage_score, effort_score, expected_records,
					expected_source_quality, priority_score, country_group
				) VALUES (?, ?, ?, 'Test', ?, ?, ?, ?, ?, ?, 0, ?)
			`, string(rune('A'+i))+"Z", "Z"+string(rune('A'+i))+"Z", tt.name,
				tt.policy, tt.coverage, tt.coverageScore, tt.effort,
				tt.expected, tt.expectedQuality, tt.countryGroup)
			if err == nil {
				t.Fatal("expected CHECK constraint failure")
			}
		})
	}
}

func TestSchemaRejectsInvalidPipelineValues(t *testing.T) {
	db := applyTestSchema(t)
	ctx := context.Background()

	countryID := insertCountry(t, db)
	sourceID := insertSource(t, db)

	t.Run("event confidence", func(t *testing.T) {
		_, err := db.ExecContext(ctx, `
			INSERT INTO events (
				date_precision, occurrence_country_id, event_type,
				investigation_status, confidence_score
			) VALUES ('unknown', ?, 'unknown', 'unknown', 101)
		`, countryID)
		if err == nil {
			t.Fatal("expected confidence CHECK constraint failure")
		}
	})

	t.Run("event dedup status", func(t *testing.T) {
		_, err := db.ExecContext(ctx, `
			INSERT INTO events (
				date_precision, occurrence_country_id, event_type,
				investigation_status, confidence_score, dedup_status
			) VALUES ('unknown', ?, 'unknown', 'unknown', 50, 'duplicate')
		`, countryID)
		if err == nil {
			t.Fatal("expected dedup_status CHECK constraint failure")
		}
	})

	eventID := insertEvent(t, db, countryID)
	validReport := `
		INSERT INTO reports (
			event_id, source_id, report_type, title, language, original_url,
			accessed_at, source_tier, extraction_status, copyright_status
		) VALUES (?, ?, 'final', 'Report', 'en', ?, 1, ?, 'pending', ?)
	`

	t.Run("report copyright status", func(t *testing.T) {
		_, err := db.ExecContext(ctx, validReport,
			eventID, sourceID, "https://example.test/report-copyright", 1, "public_domain")
		if err == nil {
			t.Fatal("expected copyright_status CHECK constraint failure")
		}
	})

	t.Run("report source tier", func(t *testing.T) {
		_, err := db.ExecContext(ctx, validReport,
			eventID, sourceID, "https://example.test/report-tier", 7, "official_public")
		if err == nil {
			t.Fatal("expected source_tier CHECK constraint failure")
		}
	})
}

func TestSchemaEnforcesForeignKeysAndAuthoritySnapshotIntegrity(t *testing.T) {
	db := applyTestSchema(t)

	if _, err := db.Exec(`
		INSERT INTO authorities (
			country_id, normalized_name, name, type, source_url, source_name
		) VALUES (9999, 'missing', 'Missing', 'national_aai', 'https://example.test', 'test')
	`); err == nil {
		t.Fatal("expected country foreign key failure")
	}

	countryID := insertCountry(t, db)
	if _, err := db.Exec(`
		INSERT INTO authorities (
			country_id, normalized_name, name, type, source_url, source_name,
			source_snapshot_id
		) VALUES (?, 'guarded', 'Guarded', 'national_aai',
			'https://example.test', 'test', 9999)
	`, countryID); err == nil {
		t.Fatal("expected authority snapshot guard failure")
	}
}

func TestApplyFSRollsBackFailedMigration(t *testing.T) {
	db := openTestDB(t)
	ctx := context.Background()
	migrationFS := fstest.MapFS{
		"sql/001_broken.sql": {
			Data: []byte(`
				CREATE TABLE rollback_probe (id INTEGER PRIMARY KEY);
				INSERT INTO table_that_does_not_exist(id) VALUES (1);
			`),
		},
	}

	if err := applyFS(ctx, db, migrationFS); err == nil {
		t.Fatal("expected migration failure")
	}

	var tableCount int
	if err := db.QueryRowContext(ctx, `
		SELECT COUNT(*) FROM sqlite_master
		WHERE type = 'table' AND name = 'rollback_probe'
	`).Scan(&tableCount); err != nil {
		t.Fatal(err)
	}
	if tableCount != 0 {
		t.Fatalf("rollback_probe tables=%d, want 0", tableCount)
	}

	var migrationCount int
	if err := db.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM schema_migrations WHERE version = 1`,
	).Scan(&migrationCount); err != nil {
		t.Fatal(err)
	}
	if migrationCount != 0 {
		t.Fatalf("migration rows=%d, want 0", migrationCount)
	}
}

func insertCountry(t *testing.T, db *sql.DB) int64 {
	t.Helper()

	result, err := db.Exec(`
		INSERT INTO countries (
			iso2, iso3, name, region, policy_status, coverage_status,
			coverage_score, effort_score, expected_records,
			expected_source_quality, priority_score
		) VALUES ('TT', 'TTT', 'Testland', 'Test', 'allowed', 'unknown', 0, 1, 0, 1, 0)
	`)
	if err != nil {
		t.Fatal(err)
	}
	id, err := result.LastInsertId()
	if err != nil {
		t.Fatal(err)
	}
	return id
}

func insertSource(t *testing.T, db *sql.DB) int64 {
	t.Helper()

	result, err := db.Exec(`
		INSERT INTO sources (
			name, url, canonical_url, source_type, source_tier
		) VALUES (
			'Test source', 'https://example.test', 'https://example.test',
			'official_aai', 1
		)
	`)
	if err != nil {
		t.Fatal(err)
	}
	id, err := result.LastInsertId()
	if err != nil {
		t.Fatal(err)
	}
	return id
}

func insertEvent(t *testing.T, db *sql.DB, countryID int64) int64 {
	t.Helper()

	result, err := db.Exec(`
		INSERT INTO events (
			date_precision, occurrence_country_id, event_type,
			investigation_status, confidence_score
		) VALUES ('unknown', ?, 'unknown', 'unknown', 50)
	`, countryID)
	if err != nil {
		t.Fatal(err)
	}
	id, err := result.LastInsertId()
	if err != nil {
		t.Fatal(err)
	}
	return id
}

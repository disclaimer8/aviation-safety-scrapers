package provenance

import (
	"context"
	"database/sql"
	"testing"
	"time"

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

// insertSource inserts a minimal sources row and returns its ID.
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

// TestPutSnapshotIsContentIdempotent verifies that inserting identical content
// twice for the same source returns the same snapshot ID and sets created=false
// on the second call. The immutability trigger must not fire (no UPDATE is
// attempted).
func TestPutSnapshotIsContentIdempotent(t *testing.T) {
	db := testDB(t)
	sourceID := insertSource(t, db)
	in := SnapshotInput{
		SourceID:    sourceID,
		SourceURL:   "https://example.test/aia",
		FinalURL:    "https://example.test/aia",
		StatusCode:  200,
		ContentType: "text/html",
		FetchedAt:   time.Unix(100, 0),
		Body:        []byte("<html>AIA</html>"),
	}

	first, created, err := PutSnapshot(context.Background(), db, in)
	if err != nil || !created {
		t.Fatalf("first: created=%v err=%v", created, err)
	}

	second, created, err := PutSnapshot(context.Background(), db, in)
	if err != nil || created || first.ID != second.ID {
		t.Fatalf("second: snapshot=%+v created=%v err=%v", second, created, err)
	}
}

// TestPutSnapshotDifferentBodyCreatesSeparateSnapshot verifies that a changed
// body produces a different checksum and therefore a distinct snapshot row.
func TestPutSnapshotDifferentBodyCreatesSeparateSnapshot(t *testing.T) {
	db := testDB(t)
	sourceID := insertSource(t, db)

	base := SnapshotInput{
		SourceID:    sourceID,
		SourceURL:   "https://example.test/aia",
		FinalURL:    "https://example.test/aia",
		StatusCode:  200,
		ContentType: "text/html",
		FetchedAt:   time.Unix(100, 0),
		Body:        []byte("<html>v1</html>"),
	}
	first, created, err := PutSnapshot(context.Background(), db, base)
	if err != nil || !created {
		t.Fatalf("first: created=%v err=%v", created, err)
	}

	base.Body = []byte("<html>v2</html>")
	second, created, err := PutSnapshot(context.Background(), db, base)
	if err != nil || !created {
		t.Fatalf("second (different body): created=%v err=%v", created, err)
	}
	if first.ID == second.ID {
		t.Fatalf("expected different snapshot IDs, got same %d", first.ID)
	}
}

// TestRunLifecycle verifies StartRun and FinishRun round-trip correctly.
func TestRunLifecycle(t *testing.T) {
	db := testDB(t)

	run, err := StartRun(context.Background(), db, "aia", "https://example.test/aia")
	if err != nil {
		t.Fatal(err)
	}
	if run.ID == 0 {
		t.Fatal("expected non-zero run ID")
	}
	if run.Status != "running" {
		t.Fatalf("initial status=%q, want running", run.Status)
	}

	err = FinishRun(context.Background(), db, run.ID, RunResult{
		Status:       "partial",
		Parsed:       4,
		Applied:      3,
		Warnings:     1,
		ErrorSummary: "one unresolved country",
	})
	if err != nil {
		t.Fatal(err)
	}

	// Verify the counts were persisted.
	var status string
	var parsed, applied, warnings int
	var errSum string
	err = db.QueryRowContext(context.Background(), `
		SELECT status, parsed_count, applied_count, warning_count, error_summary
		FROM import_runs WHERE id = ?
	`, run.ID).Scan(&status, &parsed, &applied, &warnings, &errSum)
	if err != nil {
		t.Fatalf("read run: %v", err)
	}
	if status != "partial" || parsed != 4 || applied != 3 || warnings != 1 || errSum != "one unresolved country" {
		t.Fatalf("run state: status=%q parsed=%d applied=%d warnings=%d errSum=%q",
			status, parsed, applied, warnings, errSum)
	}
}

// TestFinishRunRecordsConflicts verifies that Conflicts field is persisted.
func TestFinishRunRecordsConflicts(t *testing.T) {
	db := testDB(t)

	run, err := StartRun(context.Background(), db, "icao", "https://example.test/icao")
	if err != nil {
		t.Fatal(err)
	}

	err = FinishRun(context.Background(), db, run.ID, RunResult{
		Status:    "success",
		Parsed:    10,
		Applied:   8,
		Conflicts: 2,
	})
	if err != nil {
		t.Fatal(err)
	}

	var conflicts int
	err = db.QueryRowContext(context.Background(),
		`SELECT conflict_count FROM import_runs WHERE id = ?`, run.ID,
	).Scan(&conflicts)
	if err != nil {
		t.Fatalf("read conflicts: %v", err)
	}
	if conflicts != 2 {
		t.Fatalf("conflict_count=%d, want 2", conflicts)
	}
}

// TestPutSnapshotImmutabilityRespected confirms that calling PutSnapshot twice
// with the same body does not trigger the source_snapshots_immutable trigger.
// If any UPDATE were attempted the trigger would fire and the call would error.
func TestPutSnapshotImmutabilityRespected(t *testing.T) {
	db := testDB(t)
	sourceID := insertSource(t, db)
	in := SnapshotInput{
		SourceID:    sourceID,
		SourceURL:   "https://example.test/aia",
		FinalURL:    "https://example.test/aia",
		StatusCode:  200,
		ContentType: "text/html",
		FetchedAt:   time.Unix(200, 0),
		Body:        []byte("<html>immutable</html>"),
	}

	_, _, err := PutSnapshot(context.Background(), db, in)
	if err != nil {
		t.Fatalf("first put: %v", err)
	}
	// Second put must not error even though the immutability trigger is live.
	_, _, err = PutSnapshot(context.Background(), db, in)
	if err != nil {
		t.Fatalf("second put (immutability trigger must not fire): %v", err)
	}
}

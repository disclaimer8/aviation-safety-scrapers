package aia

import (
	"context"
	"database/sql"
	"os"
	"testing"
	"time"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/database"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/importer/common"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/migrations"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/seed"
)

const aiaSourceURL = "https://www.icao.int/safety/airnavigation/AIG/Pages/AIA-States.aspx"

// testDB returns a migrated and seeded database so the AIA source row and the
// ISO countries the importer resolves against exist.
func testDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := database.Open(t.TempDir() + "/coverage.db")
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	if err := migrations.Apply(ctx, db); err != nil {
		t.Fatal(err)
	}
	if _, err := seed.Apply(ctx, db); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := db.Close(); err != nil {
			t.Errorf("close database: %v", err)
		}
	})
	return db
}

func fixtureBody(t *testing.T) []byte {
	t.Helper()
	b, err := os.ReadFile("../../../fixtures/icao/aia.html")
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func TestImportStagesAppliesAndReturnsPartialForUnknownRecord(t *testing.T) {
	db := testDB(t)
	result, err := Import(context.Background(), db, common.Input{
		SourceURL: aiaSourceURL,
		Body:      fixtureBody(t),
		FetchedAt: time.Unix(100, 0),
	})
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != "partial" || result.Applied < 6 || result.Warnings == 0 {
		t.Fatalf("result=%+v", result)
	}

	// Every parsed record, including delegations and the malformed block, must be
	// staged — none silently dropped.
	var staged int
	if err := db.QueryRow(`SELECT COUNT(*) FROM staged_authorities WHERE import_run_id = ?`,
		result.RunID).Scan(&staged); err != nil {
		t.Fatal(err)
	}
	if staged != result.Parsed {
		t.Fatalf("staged=%d, parsed=%d: every record must be staged", staged, result.Parsed)
	}

	var raw string
	if err := db.QueryRow(`SELECT raw_contact FROM staged_authorities
		WHERE country_label = 'Angola' ORDER BY id DESC LIMIT 1`).Scan(&raw); err != nil {
		t.Fatal(err)
	}
	if raw == "" {
		t.Fatal("missing raw contact for Angola")
	}

	// The applied Angola authority carries its website via field provenance.
	var website string
	if err := db.QueryRow(`
		SELECT a.website_url FROM authorities a
		JOIN countries c ON c.id = a.country_id
		WHERE c.name = 'Angola'`).Scan(&website); err != nil {
		t.Fatal(err)
	}
	if website != "https://initpat.gov.ao" {
		t.Fatalf("Angola website=%q applied to canonical authority", website)
	}
}

func TestImportErrorPathNoDeadlock(t *testing.T) {
	db := testDB(t)
	body := fixtureBody(t)

	// Break the schema so that the first in-transaction write (staged_authorities)
	// fails with a real SQL error. We drop the table AFTER seeding so the source
	// and country lookups (which happen before BeginTx) still work.
	if _, err := db.Exec(`DROP TABLE staged_authorities`); err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()

	start := time.Now()
	result, err := Import(ctx, db, common.Input{
		SourceURL: aiaSourceURL,
		Body:      body,
		FetchedAt: time.Unix(100, 0),
	})
	elapsed := time.Since(start)

	// Must return an error quickly — well under the 3-second timeout.
	// Against the unfixed code this blocks until the 3s ctx deadline fires.
	if err == nil {
		t.Fatal("expected error from broken schema, got nil")
	}
	if elapsed >= 2*time.Second {
		t.Fatalf("Import took %v — possible deadlock (want < 2s)", elapsed)
	}
	if result.Status != "failed" {
		t.Fatalf("result.Status=%q want failed", result.Status)
	}

	// The import_runs row must be marked failed (not left as 'running').
	var status string
	if err := db.QueryRow(`SELECT status FROM import_runs WHERE id = ?`, result.RunID).Scan(&status); err != nil {
		t.Fatal(err)
	}
	if status != "failed" {
		t.Fatalf("import_run status=%q want failed", status)
	}

	// No canonical authority rows must have been committed.
	var count int
	if err := db.QueryRow(`SELECT COUNT(*) FROM authorities`).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 0 {
		t.Fatalf("authorities count=%d want 0 (rollback must have cleaned up)", count)
	}
}

// TestImportZeroRecordsGuard asserts that feeding a valid HTML page whose
// Country/Address table has only a header row (zero data rows) is treated as a
// hard failure, not a silent success with Parsed=0. This exercises the
// 0-records guard that catches Cloudflare block pages or wrong-URL responses
// that return well-formed HTML but no actual directory data. Nothing is applied
// to the canonical model, and Status is "failed".
func TestImportZeroRecordsGuard(t *testing.T) {
	db := testDB(t)

	// A page whose table has the correct Country/Address header but no data rows
	// — simulates a block or restructured page. Parse succeeds with 0 records,
	// which the importer must reject as a failure.
	headerOnlyPage := []byte(`<!DOCTYPE html><html><head><title>AIA States</title></head>
<body>
<table>
<tr><th>Country</th><th>Address</th></tr>
</table>
</body></html>`)

	result, err := Import(context.Background(), db, common.Input{
		SourceURL: aiaSourceURL,
		Body:      headerOnlyPage,
		FetchedAt: time.Unix(100, 0),
	})

	if err == nil {
		t.Fatal("expected error for zero-record page, got nil")
	}
	if result.Status != "failed" {
		t.Fatalf("result.Status=%q want failed", result.Status)
	}
	if result.Parsed != 0 {
		t.Fatalf("result.Parsed=%d want 0", result.Parsed)
	}

	// The import run must be recorded as failed.
	var status string
	if err := db.QueryRow(`SELECT status FROM import_runs WHERE id = ?`, result.RunID).Scan(&status); err != nil {
		t.Fatal(err)
	}
	if status != "failed" {
		t.Fatalf("import_run status=%q want failed", status)
	}

	// No canonical authorities may have been written.
	var count int
	if err := db.QueryRow(`SELECT COUNT(*) FROM authorities`).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 0 {
		t.Fatalf("authorities count=%d want 0 after zero-record failure", count)
	}
}

func TestImportIdenticalBodyIsUnchanged(t *testing.T) {
	db := testDB(t)
	body := fixtureBody(t)
	in := common.Input{SourceURL: aiaSourceURL, Body: body, FetchedAt: time.Unix(100, 0)}

	if _, err := Import(context.Background(), db, in); err != nil {
		t.Fatal(err)
	}
	second, err := Import(context.Background(), db, common.Input{
		SourceURL: aiaSourceURL,
		Body:      body,
		FetchedAt: time.Unix(200, 0),
	})
	if err != nil {
		t.Fatal(err)
	}
	if !second.Unchanged || second.Status != "unchanged" {
		t.Fatalf("second import=%+v, want unchanged", second)
	}

	var snapshots int
	if err := db.QueryRow(`SELECT COUNT(*) FROM source_snapshots`).Scan(&snapshots); err != nil {
		t.Fatal(err)
	}
	if snapshots != 1 {
		t.Fatalf("snapshots=%d, want exactly 1 for identical bodies", snapshots)
	}
}

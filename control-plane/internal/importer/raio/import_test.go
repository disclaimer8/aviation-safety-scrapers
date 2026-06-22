package raio

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

// raioSourceURL is the seeded ICAO RAIO directory canonical URL.
const raioSourceURL = "https://www.icao.int/safety/airnavigation/AIG/Pages/Regional-Accident-Incident-Investigation-Organizations.aspx"

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
	b, err := os.ReadFile("../../../fixtures/icao/raio.html")
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func membershipCount(t *testing.T, db *sql.DB, code string) int {
	t.Helper()
	var n int
	err := db.QueryRow(`
		SELECT COUNT(*) FROM regional_body_members m
		JOIN regional_bodies b ON b.id = m.regional_body_id
		WHERE b.code = ? AND m.role = 'member'`, code).Scan(&n)
	if err != nil {
		t.Fatal(err)
	}
	return n
}

func coverageStatus(t *testing.T, db *sql.DB, name string) string {
	t.Helper()
	var s string
	if err := db.QueryRow(`SELECT coverage_status FROM countries WHERE name = ?`, name).Scan(&s); err != nil {
		t.Fatal(err)
	}
	return s
}

func setCoverage(t *testing.T, db *sql.DB, name, status string) {
	t.Helper()
	if _, err := db.Exec(`UPDATE countries SET coverage_status = ? WHERE name = ?`, status, name); err != nil {
		t.Fatal(err)
	}
}

func TestImportRAIOPreservesCuratedECCAAAndAppliesICAOMembers(t *testing.T) {
	db := testDB(t)
	before := membershipCount(t, db, "ECCAA")

	result, err := Import(context.Background(), db, common.Input{
		SourceURL: raioSourceURL,
		Body:      fixtureBody(t),
		FetchedAt: time.Unix(100, 0),
	})
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != "success" && result.Status != "partial" {
		t.Fatalf("result=%+v", result)
	}

	// Curated ECCAA membership is never deleted: ICAO did not mention ECCAA, so it
	// must be untouched.
	if got := membershipCount(t, db, "ECCAA"); got != before {
		t.Fatalf("ECCAA changed from %d to %d", before, got)
	}
	// BAGAIA ends with exactly its seven members.
	if got := membershipCount(t, db, "BAGAIA"); got != 7 {
		t.Fatalf("BAGAIA=%d", got)
	}

	// Every parsed body is staged — none silently dropped.
	var staged int
	if err := db.QueryRow(`SELECT COUNT(*) FROM staged_regional_bodies WHERE import_run_id = ?`,
		result.RunID).Scan(&staged); err != nil {
		t.Fatal(err)
	}
	if staged != result.Parsed {
		t.Fatalf("staged=%d parsed=%d: every body must be staged", staged, result.Parsed)
	}
}

func TestImportRAIOConditionalCoverage(t *testing.T) {
	db := testDB(t)

	// Nigeria is a BAGAIA (RAIO) member seeded with coverage 'unknown' → becomes
	// regional_raio. Ghana we pin to 'direct_public_archive' → must NOT downgrade.
	if got := coverageStatus(t, db, "Nigeria"); got != "unknown" {
		t.Fatalf("Nigeria precondition coverage=%q want unknown", got)
	}
	setCoverage(t, db, "Ghana", "direct_public_archive")

	// Jordan is a real ARCM-MENA (ICM) member with coverage 'unknown'. ICM
	// membership must never change coverage.
	if got := coverageStatus(t, db, "Jordan"); got != "unknown" {
		t.Fatalf("Jordan precondition coverage=%q want unknown", got)
	}

	if _, err := Import(context.Background(), db, common.Input{
		SourceURL: raioSourceURL,
		Body:      fixtureBody(t),
		FetchedAt: time.Unix(100, 0),
	}); err != nil {
		t.Fatal(err)
	}

	if got := coverageStatus(t, db, "Nigeria"); got != "regional_raio" {
		t.Fatalf("Nigeria coverage=%q want regional_raio", got)
	}
	if got := coverageStatus(t, db, "Ghana"); got != "direct_public_archive" {
		t.Fatalf("Ghana coverage=%q must not be downgraded", got)
	}
	if got := coverageStatus(t, db, "Jordan"); got != "unknown" {
		t.Fatalf("Jordan coverage=%q ICM membership must not change coverage", got)
	}
}

func TestImportRAIOIdenticalBodyIsUnchanged(t *testing.T) {
	db := testDB(t)
	body := fixtureBody(t)
	in := common.Input{SourceURL: raioSourceURL, Body: body, FetchedAt: time.Unix(100, 0)}

	if _, err := Import(context.Background(), db, in); err != nil {
		t.Fatal(err)
	}
	second, err := Import(context.Background(), db, common.Input{
		SourceURL: raioSourceURL,
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

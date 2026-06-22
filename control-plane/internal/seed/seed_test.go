package seed

import (
	"context"
	"database/sql"
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/database"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/migrations"
)

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

func TestParseAndValidateGuardExact249(t *testing.T) {
	countries, _, err := parseAndValidate()
	if err != nil {
		t.Fatalf("parseAndValidate: %v", err)
	}
	if len(countries) != 249 {
		t.Fatalf("parseAndValidate returned %d countries, want 249", len(countries))
	}
}

func TestSeedIsIdempotentForAllTables(t *testing.T) {
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
	if first.RegionalBodies != second.RegionalBodies {
		t.Fatalf("regional bodies not idempotent: %d vs %d", first.RegionalBodies, second.RegionalBodies)
	}
	if first.Sources != second.Sources {
		t.Fatalf("sources not idempotent: %d vs %d", first.Sources, second.Sources)
	}
	if first.AircraftOriginRoutes != second.AircraftOriginRoutes {
		t.Fatalf("aircraft origin routes not idempotent: %d vs %d", first.AircraftOriginRoutes, second.AircraftOriginRoutes)
	}
}

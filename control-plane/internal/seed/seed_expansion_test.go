package seed

import (
	"context"
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/database"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/migrations"
)

// TestSeedExpansionBatch asserts the Africa+LATAM coverage-expansion overlays
// load: every authored country has a real (non-unknown) coverage_status and a
// country_group, and the subset with a verified Wayback target has it set.
func TestSeedExpansionBatch(t *testing.T) {
	ctx := context.Background()
	db, err := database.Open(t.TempDir() + "/coverage.db")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if err := migrations.Apply(ctx, db); err != nil {
		t.Fatal(err)
	}
	if _, err := Apply(ctx, db); err != nil {
		t.Fatal(err)
	}

	authored := []string{"SN", "CI", "ML", "BF", "NE", "TG", "BJ", "MR", "TD", "CF", "GA", "CG", "CD", "GQ", "AO", "KE", "TZ", "UG", "ET", "RW", "BI", "SD", "SO", "ZM", "ZW", "MW", "BW", "NA", "MA", "DZ", "TN", "CL", "CO", "EC", "PE", "UY", "SV", "CR", "GT", "DO", "JM", "NI", "TT", "SR", "VE", "CU"}
	for _, iso2 := range authored {
		var coverage string
		var group *string
		if err := db.QueryRowContext(ctx,
			`SELECT coverage_status, country_group FROM countries WHERE iso2 = ?`, iso2).
			Scan(&coverage, &group); err != nil {
			t.Errorf("%s: %v", iso2, err)
			continue
		}
		if coverage == "unknown" {
			t.Errorf("%s coverage_status = unknown, want an authored status", iso2)
		}
		if group == nil {
			t.Errorf("%s country_group is NULL, want an authored group", iso2)
		}
	}

	withTarget := []string{"BJ", "CG", "KE", "TZ", "ET", "RW", "ZM", "NA", "MA", "EC", "PE", "UY", "SV", "NI"}
	for _, iso2 := range withTarget {
		var target *string
		if err := db.QueryRowContext(ctx,
			`SELECT wayback_target FROM countries WHERE iso2 = ?`, iso2).Scan(&target); err != nil {
			t.Errorf("%s: %v", iso2, err)
			continue
		}
		if target == nil || *target == "" {
			t.Errorf("%s wayback_target is NULL, want a verified domain", iso2)
		}
	}
}

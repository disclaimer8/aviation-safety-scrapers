package seed

import (
	"context"
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/database"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/migrations"
)

// TestSeedExpansionBatch3 asserts the Europe/North-America/ZA coverage-expansion
// overlays load with a real coverage_status + country_group, and the verified
// Wayback-target subset has it set.
func TestSeedExpansionBatch3(t *testing.T) {
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

	authored := []string{"GB", "FR", "DE", "IT", "ES", "NL", "BE", "CH", "AT", "PT", "IE", "LU", "GR", "SE", "NO", "FI", "DK", "IS", "PL", "CZ", "SK", "HU", "RO", "BG", "UA", "GE", "MD", "US", "CA", "ZA", "MX", "HR", "RS", "SI", "EE", "LV", "LT", "CY", "MT", "AL", "MK", "ME", "BA", "BS", "BB", "HT", "AG"}
	for _, iso2 := range authored {
		var coverage string
		var group *string
		if err := db.QueryRowContext(ctx,
			`SELECT coverage_status, country_group FROM countries WHERE iso2 = ?`, iso2).Scan(&coverage, &group); err != nil {
			t.Errorf("%s: %v", iso2, err)
			continue
		}
		if coverage == "unknown" {
			t.Errorf("%s coverage_status = unknown, want authored", iso2)
		}
		if group == nil {
			t.Errorf("%s country_group is NULL", iso2)
		}
	}

	withTarget := []string{"IS", "UA", "AL", "BA"}
	for _, iso2 := range withTarget {
		var target *string
		if err := db.QueryRowContext(ctx,
			`SELECT wayback_target FROM countries WHERE iso2 = ?`, iso2).Scan(&target); err != nil {
			t.Errorf("%s: %v", iso2, err)
			continue
		}
		if target == nil || *target == "" {
			t.Errorf("%s wayback_target is NULL", iso2)
		}
	}
}

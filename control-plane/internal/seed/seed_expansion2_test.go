package seed

import (
	"context"
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/database"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/migrations"
)

// TestSeedExpansionBatch2 asserts the Asia/Oceania/MENA coverage-expansion
// overlays load: every authored country has a real (non-unknown) coverage_status
// and a country_group, and the subset with a verified Wayback target has it set.
func TestSeedExpansionBatch2(t *testing.T) {
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

	authored := []string{"IN", "PK", "BD", "LK", "NP", "BT", "MV", "ID", "MY", "TH", "VN", "PH", "MM", "KH", "LA", "BN", "TL", "JP", "KR", "TW", "CN", "MN", "UZ", "AE", "IL", "TR", "SA", "EG", "QA", "OM", "KW", "BH", "JO", "LB", "IR", "IQ", "YE", "LY", "AU", "NZ", "PG", "FJ", "SB", "VU", "WS", "TO", "KI", "TV", "NR", "FM", "MH", "PW"}
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

	withTarget := []string{"IN", "PK", "BD", "LK", "ID", "PH", "MM", "JP", "TW", "CN", "MN", "AE", "EG", "KW", "JO", "IR", "FJ"}
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

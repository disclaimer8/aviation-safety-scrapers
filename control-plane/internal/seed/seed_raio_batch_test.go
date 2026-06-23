package seed

import (
	"context"
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/database"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/migrations"
)

func TestSeedRAIOBatch(t *testing.T) {
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

	members := []string{
		"DM", "GD", "KN", "LC", "VC",
		"CV", "GM", "GH", "GN", "LR", "NG", "SL",
		"RU", "AM", "AZ", "BY", "KZ", "KG", "TJ", "TM",
	}
	for _, iso2 := range members {
		var coverage string
		var priority float64
		var group *string
		if err := db.QueryRowContext(ctx,
			`SELECT coverage_status, priority_score, country_group
			   FROM countries WHERE iso2 = ?`, iso2).
			Scan(&coverage, &priority, &group); err != nil {
			t.Errorf("%s: %v", iso2, err)
			continue
		}
		if coverage != "regional_raio" {
			t.Errorf("%s coverage_status = %q, want regional_raio", iso2, coverage)
		}
		if priority <= 0 {
			t.Errorf("%s priority_score = %v, want > 0", iso2, priority)
		}
		if group == nil {
			t.Errorf("%s country_group is NULL, want C/D", iso2)
		}
	}
}

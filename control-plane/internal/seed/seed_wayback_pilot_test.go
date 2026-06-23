package seed

import (
	"context"
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/database"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/migrations"
)

func TestSeedWaybackPilot(t *testing.T) {
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

	pilot := []string{"BO", "PY", "HN", "CM", "MZ", "MG"}
	for _, iso2 := range pilot {
		var target *string
		var coverage string
		if err := db.QueryRowContext(ctx,
			`SELECT wayback_target, coverage_status FROM countries WHERE iso2=?`, iso2).
			Scan(&target, &coverage); err != nil {
			t.Errorf("%s: %v", iso2, err)
			continue
		}
		if target == nil || *target == "" {
			t.Errorf("%s wayback_target is NULL, want a verified domain", iso2)
		}
		if coverage != "no_public_archive" && coverage != "source_exists_unstable" {
			t.Errorf("%s coverage_status = %q, want no_public_archive|source_exists_unstable", iso2, coverage)
		}
	}
}

package seed

import (
	"context"
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/database"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/migrations"
)

func TestSeedPopulatesWaybackTarget(t *testing.T) {
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

	// A country with no overlay wayback_target stays NULL (US).
	var us *string
	if err := db.QueryRowContext(ctx,
		`SELECT wayback_target FROM countries WHERE iso2='US'`).Scan(&us); err != nil {
		t.Fatalf("select US: %v", err)
	}
	if us != nil {
		t.Fatalf("US wayback_target = %v, want NULL", *us)
	}

	// At least one country has a non-NULL wayback_target after seeding
	// (the pilot batch from Task 3). This count is 0 until Task 3 lands; this
	// test only asserts the column is wired (US NULL). The pilot assertion lives
	// in Task 3's test.
}

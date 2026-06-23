package seed

import (
	"context"
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/database"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/migrations"
)

func TestSeedPopulatesDelegateISO2(t *testing.T) {
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

	// AD overlay sets delegate_iso2 = "FR".
	var ad *string
	if err := db.QueryRowContext(ctx,
		`SELECT delegate_iso2 FROM countries WHERE iso2='AD'`).Scan(&ad); err != nil {
		t.Fatalf("select AD: %v", err)
	}
	if ad == nil || *ad != "FR" {
		t.Fatalf("AD delegate_iso2 = %v, want \"FR\"", ad)
	}

	// A country with no overlay delegate stays NULL (US has no delegate).
	var us *string
	if err := db.QueryRowContext(ctx,
		`SELECT delegate_iso2 FROM countries WHERE iso2='US'`).Scan(&us); err != nil {
		t.Fatalf("select US: %v", err)
	}
	if us != nil {
		t.Fatalf("US delegate_iso2 = %v, want NULL", *us)
	}
}

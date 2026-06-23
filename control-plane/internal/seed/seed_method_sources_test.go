package seed

import (
	"context"
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/database"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/migrations"
)

func TestSeedHasMethodSources(t *testing.T) {
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

	wantNames := []string{
		"Authority Health Check (method)",
		"Authority Archive Crawl (method)",
		"Wayback Machine CDX (method)",
		"Scholarly PDF Discovery (method)",
		"Direct Authority Request (method)",
		"NTSB Foreign Investigations (method)",
		"BEA Foreign Investigations (method)",
		"ATSB Foreign Investigations (method)",
	}
	for _, name := range wantNames {
		var id int
		if err := db.QueryRowContext(ctx,
			`SELECT id FROM sources WHERE name = ?`, name).Scan(&id); err != nil {
			t.Errorf("method source %q not seeded: %v", name, err)
		}
	}
}

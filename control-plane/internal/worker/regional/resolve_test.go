package regional

import (
	"context"
	"database/sql"
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/database"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/migrations"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/seed"
)

func seededRegionalDB(t *testing.T) (context.Context, *sql.DB) {
	t.Helper()
	ctx := context.Background()
	db, err := database.Open(t.TempDir() + "/coverage.db")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if err := migrations.Apply(ctx, db); err != nil {
		t.Fatal(err)
	}
	if _, err := seed.Apply(ctx, db); err != nil {
		t.Fatal(err)
	}
	return ctx, db
}

func countryID(t *testing.T, ctx context.Context, db *sql.DB, iso2 string) int64 {
	t.Helper()
	var id int64
	if err := db.QueryRowContext(ctx, `SELECT id FROM countries WHERE iso2=?`, iso2).Scan(&id); err != nil {
		t.Fatal(err)
	}
	return id
}

func TestResolveBody(t *testing.T) {
	ctx, db := seededRegionalDB(t)
	cases := map[string]string{"NG": "BAGAIA", "RU": "IAC", "LC": "ECCAA"}
	for iso2, wantBody := range cases {
		got, ok, err := ResolveBody(ctx, db, countryID(t, ctx, db, iso2))
		if err != nil || !ok {
			t.Errorf("%s: ResolveBody = (%q,%v,%v)", iso2, got, ok, err)
			continue
		}
		if got != wantBody {
			t.Errorf("%s body = %q, want %q", iso2, got, wantBody)
		}
	}
	// US is not a regional-body member → no body.
	got, ok, err := ResolveBody(ctx, db, countryID(t, ctx, db, "US"))
	if err != nil {
		t.Fatal(err)
	}
	if ok || got != "" {
		t.Fatalf("US ResolveBody = (%q,%v), want (\"\",false)", got, ok)
	}
}

package wayback

import (
	"context"
	"database/sql"
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/database"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/migrations"
)

func waybackTestDB(t *testing.T) (context.Context, *sql.DB) {
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
	return ctx, db
}

func insertCountry(t *testing.T, ctx context.Context, db *sql.DB, iso2 string, target *string) int64 {
	t.Helper()
	res, err := db.ExecContext(ctx, `
		INSERT INTO countries
			(iso2, iso3, name, region, policy_status, coverage_status,
			 coverage_score, effort_score, wayback_target)
		VALUES (?, ?, 'N','R','allowed','no_public_archive',1,3, ?)`,
		iso2, iso2+"X", target)
	if err != nil {
		t.Fatal(err)
	}
	id, _ := res.LastInsertId()
	return id
}

func TestResolveTargetPrefersOverlay(t *testing.T) {
	ctx, db := waybackTestDB(t)
	target := "caa.aa.gov"
	id := insertCountry(t, ctx, db, "AA", &target)

	got, ok, err := ResolveTarget(ctx, db, id)
	if err != nil || !ok {
		t.Fatalf("ResolveTarget = (%q,%v,%v)", got, ok, err)
	}
	if got != "caa.aa.gov" {
		t.Fatalf("target = %q, want caa.aa.gov", got)
	}
}

func TestResolveTargetFallsBackToAuthority(t *testing.T) {
	ctx, db := waybackTestDB(t)
	id := insertCountry(t, ctx, db, "BB", nil) // no overlay target
	if _, err := db.ExecContext(ctx, `
		INSERT INTO authorities
			(country_id, normalized_name, name, type, archive_url, source_url, source_name)
		VALUES (?, 'aai', 'AAI', 'national_aai', 'archive.bb.gov', 'https://bb/', 'seed')`, id); err != nil {
		t.Fatal(err)
	}
	got, ok, err := ResolveTarget(ctx, db, id)
	if err != nil || !ok {
		t.Fatalf("ResolveTarget = (%q,%v,%v)", got, ok, err)
	}
	if got != "archive.bb.gov" {
		t.Fatalf("target = %q, want archive.bb.gov", got)
	}
}

func TestResolveTargetNoneWhenNeither(t *testing.T) {
	ctx, db := waybackTestDB(t)
	id := insertCountry(t, ctx, db, "CC", nil)
	got, ok, err := ResolveTarget(ctx, db, id)
	if err != nil {
		t.Fatal(err)
	}
	if ok || got != "" {
		t.Fatalf("ResolveTarget = (%q,%v), want (\"\",false)", got, ok)
	}
}

func insertCountryPriority(t *testing.T, ctx context.Context, db *sql.DB, iso2 string, target *string, priority float64) int64 {
	t.Helper()
	res, err := db.ExecContext(ctx, `
		INSERT INTO countries
			(iso2, iso3, name, region, policy_status, coverage_status,
			 coverage_score, effort_score, priority_score, wayback_target)
		VALUES (?, ?, 'N','R','allowed','no_public_archive',1,3, ?, ?)`,
		iso2, iso2+"X", priority, target)
	if err != nil {
		t.Fatal(err)
	}
	id, _ := res.LastInsertId()
	return id
}

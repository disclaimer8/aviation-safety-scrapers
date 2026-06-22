package app_test

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/app"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/database"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/migrations"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/seed"
)

func TestRunMigrateSeedValidateExport(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "coverage.db")
	outPath := filepath.Join(t.TempDir(), "coverage.json")
	var stdout, stderr bytes.Buffer

	for _, args := range [][]string{
		{"migrate", "--db", dbPath},
		{"seed", "--db", dbPath},
		{"validate", "--db", dbPath},
		{"export", "--db", dbPath, "--format", "json", "--output", outPath,
			"--generated-at", "2026-06-22T12:00:00Z"},
	} {
		stdout.Reset()
		stderr.Reset()
		if code := app.Run(context.Background(), args, &stdout, &stderr); code != 0 {
			t.Fatalf("args=%v code=%d stderr=%s stdout=%s", args, code, stderr.String(), stdout.String())
		}
	}
	if _, err := os.Stat(outPath); err != nil {
		t.Fatal(err)
	}
}

func TestRunImportAIAFromFile(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "coverage.db")
	var stdout, stderr bytes.Buffer

	// migrate + seed
	for _, args := range [][]string{
		{"migrate", "--db", dbPath},
		{"seed", "--db", dbPath},
	} {
		stdout.Reset()
		stderr.Reset()
		if code := app.Run(context.Background(), args, &stdout, &stderr); code != 0 {
			t.Fatalf("setup args=%v code=%d stderr=%s", args, code, stderr.String())
		}
	}

	// import-aia from fixture file
	fixturePath := filepath.Join("..", "..", "fixtures", "icao", "aia.html")
	stdout.Reset()
	stderr.Reset()
	code := app.Run(context.Background(),
		[]string{"import-aia", "--db", dbPath, "--source-file", fixturePath},
		&stdout, &stderr)
	if code != 0 {
		t.Fatalf("import-aia code=%d stderr=%s stdout=%s", code, stderr.String(), stdout.String())
	}

	// JSON result must contain "Status" and "Applied" (common.Result has no json tags)
	out := stdout.String()
	if !strings.Contains(out, `"Status"`) {
		t.Fatalf("output missing 'Status': %s", out)
	}
	if !strings.Contains(out, `"Applied"`) {
		t.Fatalf("output missing 'Applied': %s", out)
	}
}

func TestValidateReturnsNonZeroOnInvariantFailure(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "coverage.db")
	var stdout, stderr bytes.Buffer

	// migrate + seed
	for _, args := range [][]string{
		{"migrate", "--db", dbPath},
		{"seed", "--db", dbPath},
	} {
		stdout.Reset()
		stderr.Reset()
		if code := app.Run(context.Background(), args, &stdout, &stderr); code != 0 {
			t.Fatalf("setup args=%v code=%d stderr=%s", args, code, stderr.String())
		}
	}

	// Insert a forbidden direct crawl job for an excluded country (e.g. AF = Afghanistan).
	db, err := database.Open(dbPath)
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	ctx := context.Background()
	if err := migrations.Apply(ctx, db); err != nil {
		t.Fatalf("re-migrate: %v", err)
	}
	if _, err := seed.Apply(ctx, db); err != nil {
		t.Fatalf("re-seed: %v", err)
	}

	// Get the id for AF (excluded country)
	var countryID int64
	if err := db.QueryRowContext(ctx, `SELECT id FROM countries WHERE iso2 = 'AF'`).Scan(&countryID); err != nil {
		t.Fatalf("lookup AF: %v", err)
	}
	// Get any seeded source id
	var sourceID int64
	if err := db.QueryRowContext(ctx, `SELECT id FROM sources LIMIT 1`).Scan(&sourceID); err != nil {
		t.Fatalf("lookup source: %v", err)
	}
	// Insert a direct crawl job — this violates invariant 8
	_, err = db.ExecContext(ctx, `
		INSERT INTO crawl_jobs (source_id, country_id, job_type, status)
		VALUES (?, ?, 'archive_crawl', 'pending')
	`, sourceID, countryID)
	if err != nil {
		t.Fatalf("insert crawl_job: %v", err)
	}
	db.Close()

	// validate should exit 1
	stdout.Reset()
	stderr.Reset()
	code := app.Run(context.Background(), []string{"validate", "--db", dbPath}, &stdout, &stderr)
	if code != 1 {
		t.Fatalf("expected exit code 1 for invariant failure, got %d; stdout=%s stderr=%s",
			code, stdout.String(), stderr.String())
	}
}

func TestRunUnknownCommandExits2(t *testing.T) {
	var stdout, stderr bytes.Buffer
	code := app.Run(context.Background(), []string{"no-such-command"}, &stdout, &stderr)
	if code != 2 {
		t.Fatalf("expected exit code 2, got %d", code)
	}
}

func TestRunMissingDBExits2(t *testing.T) {
	var stdout, stderr bytes.Buffer
	// Missing --db flag
	code := app.Run(context.Background(), []string{"migrate"}, &stdout, &stderr)
	if code != 2 {
		t.Fatalf("expected exit code 2 for missing --db, got %d", code)
	}
}

func TestRunExportInvalidFormatExits2(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "coverage.db")
	var stdout, stderr bytes.Buffer
	for _, args := range [][]string{
		{"migrate", "--db", dbPath},
		{"seed", "--db", dbPath},
	} {
		stdout.Reset()
		stderr.Reset()
		app.Run(context.Background(), args, &stdout, &stderr) //nolint
	}
	stdout.Reset()
	stderr.Reset()
	code := app.Run(context.Background(),
		[]string{"export", "--db", dbPath, "--format", "csv", "--output", "/tmp/out.csv"},
		&stdout, &stderr)
	if code != 2 {
		t.Fatalf("expected exit code 2 for bad format, got %d", code)
	}
}

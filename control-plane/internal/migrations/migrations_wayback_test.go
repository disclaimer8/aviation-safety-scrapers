package migrations

import (
	"context"
	"testing"
)

func TestMigration005WaybackSchema(t *testing.T) {
	db := applyTestSchema(t)
	ctx := context.Background()

	// wayback_target column exists and is nullable.
	if _, err := db.ExecContext(ctx, `
		INSERT INTO countries
			(iso2, iso3, name, region, policy_status, coverage_status,
			 coverage_score, effort_score, wayback_target)
		VALUES ('XW','XWW','Test W','Test','allowed','no_public_archive',1,3,'caa.example.gov')
	`); err != nil {
		t.Fatalf("insert with wayback_target: %v", err)
	}
	var got *string
	if err := db.QueryRowContext(ctx,
		`SELECT wayback_target FROM countries WHERE iso2='XW'`).Scan(&got); err != nil {
		t.Fatalf("select wayback_target: %v", err)
	}
	if got == nil || *got != "caa.example.gov" {
		t.Fatalf("wayback_target = %v, want caa.example.gov", got)
	}

	// staged_wayback_documents accepts a row and enforces UNIQUE(country_id,digest).
	var countryID int64
	if err := db.QueryRowContext(ctx, `SELECT id FROM countries WHERE iso2='XW'`).Scan(&countryID); err != nil {
		t.Fatal(err)
	}
	// crawl_jobs needs a source; reuse any country and a fake source via a job row.
	var jobID int64
	// Insert a source + crawl_job to satisfy the FK.
	res, err := db.ExecContext(ctx, `
		INSERT INTO sources (name, url, canonical_url, source_type, source_tier)
		VALUES ('t','https://t/','https://t/','wayback',5)`)
	if err != nil {
		t.Fatal(err)
	}
	srcID, _ := res.LastInsertId()
	res, err = db.ExecContext(ctx, `
		INSERT INTO crawl_jobs (source_id, country_id, job_type, status)
		VALUES (?,?, 'wayback_cdx', 'running')`, srcID, countryID)
	if err != nil {
		t.Fatal(err)
	}
	jobID, _ = res.LastInsertId()

	ins := `INSERT INTO staged_wayback_documents
		(crawl_job_id, country_id, original_url, archived_url, timestamp, mimetype, digest)
		VALUES (?,?,?,?,?,?,?)`
	if _, err := db.ExecContext(ctx, ins, jobID, countryID,
		"http://caa.example.gov/a.pdf", "https://web.archive.org/web/2010id_/http://caa.example.gov/a.pdf",
		"20100101000000", "application/pdf", "DIGEST1"); err != nil {
		t.Fatalf("insert staged doc: %v", err)
	}
	// Same (country_id, digest) must conflict.
	_, err = db.ExecContext(ctx, ins, jobID, countryID,
		"http://caa.example.gov/a.pdf", "https://web.archive.org/web/2011id_/http://caa.example.gov/a.pdf",
		"20110101000000", "application/pdf", "DIGEST1")
	if err == nil {
		t.Fatal("expected UNIQUE(country_id,digest) violation on duplicate digest")
	}
}

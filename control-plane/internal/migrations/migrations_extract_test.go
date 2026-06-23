package migrations

import (
	"context"
	"database/sql"
	"testing"

	_ "modernc.org/sqlite"
)

func TestMigration006ExtractSchema(t *testing.T) {
	ctx := context.Background()
	db, err := sql.Open("sqlite", ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if _, err := db.ExecContext(ctx, "PRAGMA foreign_keys=ON"); err != nil {
		t.Fatal(err)
	}
	if err := Apply(ctx, db); err != nil {
		t.Fatalf("apply migrations: %v", err)
	}

	// New columns exist with defaults: insert a staged doc, read back extraction_status.
	_, err = db.ExecContext(ctx, `INSERT INTO countries (iso2, iso3, name, region, policy_status, coverage_status, coverage_score, effort_score) VALUES ('XW','XWW','Testland','Test','allowed','unknown',0,1)`)
	if err != nil {
		t.Fatal(err)
	}
	var countryID, srcID int64
	db.QueryRowContext(ctx, `SELECT id FROM countries WHERE iso2='XW'`).Scan(&countryID)
	res, err := db.ExecContext(ctx, `
		INSERT INTO sources (name, url, canonical_url, source_type, source_tier, copyright_policy_notes)
		VALUES ('S','u','c','wayback',2,NULL)`)
	if err != nil {
		t.Fatal(err)
	}
	srcID, _ = res.LastInsertId()
	res, err = db.ExecContext(ctx, `
		INSERT INTO crawl_jobs (source_id, country_id, job_type, status)
		VALUES (?,?,'wayback_cdx','running')`, srcID, countryID)
	if err != nil {
		t.Fatal(err)
	}
	jobID, _ := res.LastInsertId()
	if _, err := db.ExecContext(ctx, `
		INSERT INTO staged_wayback_documents
			(crawl_job_id, country_id, original_url, archived_url, timestamp, mimetype, digest)
		VALUES (?,?,'o','a','20200101000000','application/pdf','d1')`, jobID, countryID); err != nil {
		t.Fatal(err)
	}

	var status string
	var attempts int
	if err := db.QueryRowContext(ctx, `
		SELECT extraction_status, extraction_attempts FROM staged_wayback_documents WHERE digest='d1'`).
		Scan(&status, &attempts); err != nil {
		t.Fatalf("read new columns: %v", err)
	}
	if status != "pending" || attempts != 0 {
		t.Fatalf("defaults wrong: status=%q attempts=%d", status, attempts)
	}

	// CHECK rejects an invalid extraction_status.
	_, err = db.ExecContext(ctx, `
		UPDATE staged_wayback_documents SET extraction_status='bogus' WHERE digest='d1'`)
	if err == nil {
		t.Fatal("expected CHECK to reject bogus extraction_status")
	}
}

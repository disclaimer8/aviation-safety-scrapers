package wayback

import (
	"context"
	"database/sql"
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/migrations"
	_ "modernc.org/sqlite"
)

// newExtractTestDB returns a migrated in-memory DB.
func newExtractTestDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite", ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if _, err := db.ExecContext(context.Background(), "PRAGMA foreign_keys=ON"); err != nil {
		t.Fatal(err)
	}
	if err := migrations.Apply(context.Background(), db); err != nil {
		t.Fatalf("apply migrations: %v", err)
	}
	return db
}

// seedDownloadedDoc inserts a country, a source, a crawl_job, and one
// downloaded staged document. Returns the document id and country id.
func seedDownloadedDoc(t *testing.T, db *sql.DB, iso2, digest string) (docID, countryID int64) {
	t.Helper()
	ctx := context.Background()
	res, err := db.ExecContext(ctx, `
		INSERT INTO countries
			(iso2, iso3, name, region, policy_status, coverage_status,
			 coverage_score, effort_score)
		VALUES (?, ?, ?, 'R', 'allowed', 'no_public_archive', 1, 3)`,
		iso2, iso2+"X", iso2+"land")
	if err != nil {
		t.Fatal(err)
	}
	countryID, _ = res.LastInsertId()
	res, err = db.ExecContext(ctx, `
		INSERT INTO sources (name, url, canonical_url, source_type, source_tier)
		VALUES ('seed','u','c-`+digest+`','wayback',2)`)
	if err != nil {
		t.Fatal(err)
	}
	srcID, _ := res.LastInsertId()
	res, err = db.ExecContext(ctx, `
		INSERT INTO crawl_jobs (source_id, country_id, job_type, status)
		VALUES (?,?,'wayback_cdx','running')`, srcID, countryID)
	if err != nil {
		t.Fatal(err)
	}
	jobID, _ := res.LastInsertId()
	res, err = db.ExecContext(ctx, `
		INSERT INTO staged_wayback_documents
			(crawl_job_id, country_id, original_url, archived_url, timestamp, mimetype, digest,
			 local_file_path, checksum, download_status)
		VALUES (?,?,?,?,?,?,?,?,?, 'downloaded')`,
		jobID, countryID,
		"https://caa.example/report.pdf",
		"https://web.archive.org/web/20200101id_/https://caa.example/report.pdf",
		"20200101000000", "application/pdf", digest,
		"/store/"+iso2+"/"+digest+".pdf", "checksum-"+digest)
	if err != nil {
		t.Fatal(err)
	}
	docID, _ = res.LastInsertId()
	return docID, countryID
}

package wayback

import (
	"encoding/json"
	"os"
	"testing"
)

func TestRunJobSuccessStagesAndDownloads(t *testing.T) {
	ctx, db := waybackTestDB(t)
	target := "example.gov"
	cid := insertCountry(t, ctx, db, "ZZ", &target)
	res, _ := db.ExecContext(ctx, `
		INSERT INTO sources (name, url, canonical_url, source_type, source_tier)
		VALUES ('wb','https://wb/','https://wb/','wayback',5)`)
	srcID, _ := res.LastInsertId()
	res, _ = db.ExecContext(ctx, `
		INSERT INTO crawl_jobs (source_id, country_id, job_type, status)
		VALUES (?,?, 'wayback_cdx', 'running')`, srcID, cid)
	jid, _ := res.LastInsertId()

	cdxBody, err := os.ReadFile("testdata/cdx_sample.json")
	if err != nil {
		t.Fatal(err)
	}
	f := &fixtureFetcher{CDXBody: cdxBody} // Get returns default bytes for any archived URL
	if err := RunJob(ctx, db, f, t.TempDir(), Job{ID: jid, CountryID: cid, ISO2: "ZZ"}); err != nil {
		t.Fatal(err)
	}

	var status string
	var stats string
	if err := db.QueryRowContext(ctx,
		`SELECT status, stats_json FROM crawl_jobs WHERE id=?`, jid).Scan(&status, &stats); err != nil {
		t.Fatal(err)
	}
	if status != "success" {
		t.Fatalf("status = %q, want success", status)
	}
	var s struct{ Found, Staged, Downloaded, Errors int }
	if err := json.Unmarshal([]byte(stats), &s); err != nil {
		t.Fatalf("stats_json: %v (%s)", err, stats)
	}
	// cdx_sample.json yields 2 PDF snapshots (DIGESTA, DIGESTB).
	if s.Found != 2 || s.Staged != 2 || s.Downloaded != 2 || s.Errors != 0 {
		t.Fatalf("stats = %+v, want found2 staged2 downloaded2 errors0", s)
	}
}

func TestRunJobNoTargetMarksFailed(t *testing.T) {
	ctx, db := waybackTestDB(t)
	cid := insertCountry(t, ctx, db, "ZZ", nil) // no target, no authority
	res, _ := db.ExecContext(ctx, `
		INSERT INTO sources (name, url, canonical_url, source_type, source_tier)
		VALUES ('wb','https://wb/','https://wb/','wayback',5)`)
	srcID, _ := res.LastInsertId()
	res, _ = db.ExecContext(ctx, `
		INSERT INTO crawl_jobs (source_id, country_id, job_type, status)
		VALUES (?,?, 'wayback_cdx', 'running')`, srcID, cid)
	jid, _ := res.LastInsertId()

	if err := RunJob(ctx, db, &fixtureFetcher{}, t.TempDir(), Job{ID: jid, CountryID: cid, ISO2: "ZZ"}); err != nil {
		t.Fatal(err) // RunJob itself does not error on an unresolved target; it records it
	}
	var status string
	if err := db.QueryRowContext(ctx, `SELECT status FROM crawl_jobs WHERE id=?`, jid).Scan(&status); err != nil {
		t.Fatal(err)
	}
	if status != "failed" {
		t.Fatalf("status = %q, want failed", status)
	}
	var errCount int
	if err := db.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM crawl_errors WHERE crawl_job_id=?`, jid).Scan(&errCount); err != nil {
		t.Fatal(err)
	}
	if errCount == 0 {
		t.Fatal("expected a crawl_errors row for unresolved target")
	}
}

func TestProcessPendingOrdersByPriority(t *testing.T) {
	ctx, db := waybackTestDB(t)
	// Two countries with targets; HI has higher priority_score.
	hiTarget, loTarget := "hi.gov", "lo.gov"
	hi := insertCountryPriority(t, ctx, db, "HI", &hiTarget, 100)
	lo := insertCountryPriority(t, ctx, db, "LO", &loTarget, 1)
	res, _ := db.ExecContext(ctx, `
		INSERT INTO sources (name, url, canonical_url, source_type, source_tier)
		VALUES ('wb','https://wb/','https://wb/','wayback',5)`)
	srcID, _ := res.LastInsertId()
	for _, cid := range []int64{lo, hi} {
		db.ExecContext(ctx, `INSERT INTO crawl_jobs (source_id, country_id, job_type, status)
			VALUES (?,?, 'wayback_cdx', 'pending')`, srcID, cid)
	}
	cdxBody, _ := os.ReadFile("testdata/cdx_sample.json")
	f := &fixtureFetcher{CDXBody: cdxBody}
	processed, err := ProcessPending(ctx, db, f, t.TempDir(), 1) // limit 1 → only highest priority
	if err != nil {
		t.Fatal(err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}
	// HI job must be done (success), LO still pending.
	var hiStatus, loStatus string
	db.QueryRowContext(ctx, `SELECT status FROM crawl_jobs WHERE country_id=?`, hi).Scan(&hiStatus)
	db.QueryRowContext(ctx, `SELECT status FROM crawl_jobs WHERE country_id=?`, lo).Scan(&loStatus)
	if hiStatus != "success" {
		t.Errorf("HI status = %q, want success", hiStatus)
	}
	if loStatus != "pending" {
		t.Errorf("LO status = %q, want pending (limit 1, lower priority)", loStatus)
	}
}

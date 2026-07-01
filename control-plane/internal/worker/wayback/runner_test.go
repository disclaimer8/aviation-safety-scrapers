package wayback

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strings"
	"testing"
)

func TestRunJobSuccessStagesAndDownloads(t *testing.T) {
	ctx, db := waybackTestDB(t)
	target := "example.gov"
	cid := insertCountry(t, ctx, db, "ZZ", &target)
	res, err := db.ExecContext(ctx, `
		INSERT INTO sources (name, url, canonical_url, source_type, source_tier)
		VALUES ('wb','https://wb/','https://wb/','wayback',5)`)
	if err != nil {
		t.Fatal(err)
	}
	srcID, _ := res.LastInsertId()
	res, err = db.ExecContext(ctx, `
		INSERT INTO crawl_jobs (source_id, country_id, job_type, status)
		VALUES (?,?, 'wayback_cdx', 'running')`, srcID, cid)
	if err != nil {
		t.Fatal(err)
	}
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
	res, err := db.ExecContext(ctx, `
		INSERT INTO sources (name, url, canonical_url, source_type, source_tier)
		VALUES ('wb','https://wb/','https://wb/','wayback',5)`)
	if err != nil {
		t.Fatal(err)
	}
	srcID, _ := res.LastInsertId()
	for _, cid := range []int64{lo, hi} {
		if _, err := db.ExecContext(ctx, `INSERT INTO crawl_jobs (source_id, country_id, job_type, status)
			VALUES (?,?, 'wayback_cdx', 'pending')`, srcID, cid); err != nil {
			t.Fatal(err)
		}
	}
	cdxBody, err := os.ReadFile("testdata/cdx_sample.json")
	if err != nil {
		t.Fatal(err)
	}
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

func TestProcessPendingResumesStaleRunningJob(t *testing.T) {
	ctx, db := waybackTestDB(t)

	// Stale country: running job started 2 hours ago — should be re-picked.
	staleTarget := "stale.gov"
	staleCID := insertCountryPriority(t, ctx, db, "ST", &staleTarget, 50)
	res, err := db.ExecContext(ctx, `
		INSERT INTO sources (name, url, canonical_url, source_type, source_tier)
		VALUES ('wb','https://wb/','https://wb/','wayback',5)`)
	if err != nil {
		t.Fatal(err)
	}
	srcID, _ := res.LastInsertId()
	res, err = db.ExecContext(ctx, `
		INSERT INTO crawl_jobs (source_id, country_id, job_type, status, started_at)
		VALUES (?,?, 'wayback_cdx', 'running',
		        CAST(unixepoch('subsec')*1000 AS INTEGER) - 7200000)`,
		srcID, staleCID)
	if err != nil {
		t.Fatal(err)
	}
	staleJobID, _ := res.LastInsertId()

	// Fresh country: running job started just now — must NOT be re-picked.
	freshTarget := "fresh.gov"
	freshCID := insertCountryPriority(t, ctx, db, "FR", &freshTarget, 10)
	res, err = db.ExecContext(ctx, `
		INSERT INTO crawl_jobs (source_id, country_id, job_type, status, started_at)
		VALUES (?,?, 'wayback_cdx', 'running',
		        CAST(unixepoch('subsec')*1000 AS INTEGER))`,
		srcID, freshCID)
	if err != nil {
		t.Fatal(err)
	}
	freshJobID, _ := res.LastInsertId()

	cdxBody, err := os.ReadFile("testdata/cdx_sample.json")
	if err != nil {
		t.Fatal(err)
	}
	f := &fixtureFetcher{CDXBody: cdxBody}

	processed, err := ProcessPending(ctx, db, f, t.TempDir(), 0)
	if err != nil {
		t.Fatal(err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1 (only stale job)", processed)
	}

	// Stale job must be finalized (success or partial).
	var staleStatus string
	if err := db.QueryRowContext(ctx, `SELECT status FROM crawl_jobs WHERE id=?`, staleJobID).Scan(&staleStatus); err != nil {
		t.Fatal(err)
	}
	if staleStatus != "success" && staleStatus != "partial" {
		t.Errorf("stale job status = %q, want success or partial", staleStatus)
	}

	// Fresh job must remain running — not touched.
	var freshStatus string
	if err := db.QueryRowContext(ctx, `SELECT status FROM crawl_jobs WHERE id=?`, freshJobID).Scan(&freshStatus); err != nil {
		t.Fatal(err)
	}
	if freshStatus != "running" {
		t.Errorf("fresh job status = %q, want running (recent started_at, must not be re-picked)", freshStatus)
	}
}

// captureStderr redirects os.Stderr for the duration of fn and returns
// everything written to it. Used to assert on the GO-CP-4 tripwire token.
func captureStderr(t *testing.T, fn func()) string {
	t.Helper()
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	orig := os.Stderr
	os.Stderr = w
	defer func() { os.Stderr = orig }()

	fn()

	w.Close()
	var buf bytes.Buffer
	io.Copy(&buf, r)
	return buf.String()
}

// TestRunJobZeroFoundEmitsSilentFailTripwire pins GO-CP-4 for the wayback
// worker: a job that completes cleanly (target resolved, CDX fetched, parsed)
// but finds zero snapshots must go 'partial' with a stats_json warning and
// print the grep-able SILENT_FAIL_SUSPECT token, instead of finalizing as an
// indistinguishable 'success'.
func TestRunJobZeroFoundEmitsSilentFailTripwire(t *testing.T) {
	ctx, db := waybackTestDB(t)
	target := "example.gov"
	cid := insertCountry(t, ctx, db, "ZZ", &target)
	res, err := db.ExecContext(ctx, `
		INSERT INTO sources (name, url, canonical_url, source_type, source_tier)
		VALUES ('wb','https://wb/','https://wb/','wayback',5)`)
	if err != nil {
		t.Fatal(err)
	}
	srcID, _ := res.LastInsertId()
	res, err = db.ExecContext(ctx, `
		INSERT INTO crawl_jobs (source_id, country_id, job_type, status)
		VALUES (?,?, 'wayback_cdx', 'running')`, srcID, cid)
	if err != nil {
		t.Fatal(err)
	}
	jid, _ := res.LastInsertId()

	f := &fixtureFetcher{CDXBody: []byte("[]")}
	out := captureStderr(t, func() {
		if err := RunJob(ctx, db, f, t.TempDir(), Job{ID: jid, CountryID: cid, ISO2: "ZZ"}); err != nil {
			t.Fatal(err)
		}
	})

	if !strings.Contains(out, "SILENT_FAIL_SUSPECT") || !strings.Contains(out, fmt.Sprintf("job=%d", jid)) || !strings.Contains(out, "found=0") {
		t.Fatalf("expected SILENT_FAIL_SUSPECT tripwire on stderr, got: %q", out)
	}

	var status, stats string
	db.QueryRowContext(ctx, `SELECT status, stats_json FROM crawl_jobs WHERE id=?`, jid).Scan(&status, &stats)
	if status != "partial" {
		t.Fatalf("status = %q, want partial", status)
	}
	var s struct {
		Found   int
		Warning string
	}
	json.Unmarshal([]byte(stats), &s)
	if s.Found != 0 || s.Warning != "found_zero" {
		t.Fatalf("stats = %+v, want Found=0 Warning=found_zero", s)
	}
}

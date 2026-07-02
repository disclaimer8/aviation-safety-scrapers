package regional

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
	"testing"
)

// insertRegionalJob inserts a regional_raio country that is a real member of the
// given body (so ResolveBody finds it), a source, and an archive_crawl job.
func insertRegionalJob(t *testing.T, ctx context.Context, db *sql.DB, iso2 string, status string, startedAgoMs int64) (int64, int64) {
	t.Helper()
	// iso2 must be a real seeded regional-body member (e.g. NG=BAGAIA, RU=IAC, LC=ECCAA).
	// Force coverage_status to regional_raio for determinism.
	if _, err := db.ExecContext(ctx, `UPDATE countries SET coverage_status='regional_raio' WHERE iso2=?`, iso2); err != nil {
		t.Fatal(err)
	}
	cid := countryID(t, ctx, db, iso2)
	res, _ := db.ExecContext(ctx, `INSERT INTO sources (name,url,canonical_url,source_type,source_tier)
		VALUES (?, ?, ?, 'regional_body',4)`, "s"+iso2, "https://s/"+iso2, "https://s/"+iso2)
	srcID, _ := res.LastInsertId()
	res, err := db.ExecContext(ctx, `INSERT INTO crawl_jobs (source_id,country_id,job_type,status) VALUES (?,?, 'archive_crawl', ?)`, srcID, cid, status)
	if err != nil {
		t.Fatal(err)
	}
	jid, _ := res.LastInsertId()
	if startedAgoMs > 0 {
		db.ExecContext(ctx, `UPDATE crawl_jobs SET started_at = (CAST(unixepoch('subsec')*1000 AS INTEGER) - ?) WHERE id=?`, startedAgoMs, jid)
	}
	return cid, jid
}

func TestRunJobSuccessStages(t *testing.T) {
	ctx, db := seededRegionalDB(t)
	cid, jid := insertRegionalJob(t, ctx, db, "RU", "running", 0)
	clients := Clients{IAC: &fixtureClient{Records: []RegionalRecord{
		{Ref: "2024-RA-01", Title: "A", OriginalURL: "https://mak.aero/1"},
		{Ref: "2024-RA-02", Title: "B", OriginalURL: "https://mak.aero/2"},
	}}}
	if err := RunJob(ctx, db, clients, Job{ID: jid, CountryID: cid, ISO2: "RU", BodyCode: "IAC"}); err != nil {
		t.Fatal(err)
	}
	var status, stats string
	db.QueryRowContext(ctx, `SELECT status, stats_json FROM crawl_jobs WHERE id=?`, jid).Scan(&status, &stats)
	if status != "success" {
		t.Fatalf("status = %q, want success", status)
	}
	var s struct{ Found, Staged, Errors int }
	json.Unmarshal([]byte(stats), &s)
	if s.Found != 2 || s.Staged != 2 || s.Errors != 0 {
		t.Fatalf("stats = %+v", s)
	}
}

func TestRunJobSearchErrorFails(t *testing.T) {
	ctx, db := seededRegionalDB(t)
	cid, jid := insertRegionalJob(t, ctx, db, "RU", "running", 0)
	clients := Clients{IAC: &fixtureClient{Err: errors.New("boom")}}
	if err := RunJob(ctx, db, clients, Job{ID: jid, CountryID: cid, ISO2: "RU", BodyCode: "IAC"}); err != nil {
		t.Fatal(err)
	}
	var status string
	db.QueryRowContext(ctx, `SELECT status FROM crawl_jobs WHERE id=?`, jid).Scan(&status)
	if status != "failed" {
		t.Fatalf("status = %q, want failed", status)
	}
	var n int
	db.QueryRowContext(ctx, `SELECT COUNT(*) FROM crawl_errors WHERE crawl_job_id=?`, jid).Scan(&n)
	if n == 0 {
		t.Fatal("expected a crawl_errors row")
	}
}

func TestRunJobUnknownBodyFails(t *testing.T) {
	ctx, db := seededRegionalDB(t)
	cid, jid := insertRegionalJob(t, ctx, db, "RU", "running", 0)
	// No IAC client wired → clientFor returns false → failed + crawl_errors.
	if err := RunJob(ctx, db, Clients{}, Job{ID: jid, CountryID: cid, ISO2: "RU", BodyCode: "IAC"}); err != nil {
		t.Fatal(err)
	}
	var status string
	db.QueryRowContext(ctx, `SELECT status FROM crawl_jobs WHERE id=?`, jid).Scan(&status)
	if status != "failed" {
		t.Fatalf("status = %q, want failed", status)
	}
}

func TestProcessPendingOnlyRegionalRaioAndResumesStale(t *testing.T) {
	ctx, db := seededRegionalDB(t)
	// regional_raio member (RU=IAC) stale-running 2h → re-picked.
	_, staleJid := insertRegionalJob(t, ctx, db, "RU", "running", 7200000)
	// a non-regional archive_crawl job (force direct_public_archive) must NOT be selected.
	db.ExecContext(ctx, `UPDATE countries SET coverage_status='direct_public_archive' WHERE iso2='US'`)
	usCid := countryID(t, ctx, db, "US")
	res, _ := db.ExecContext(ctx, `INSERT INTO sources (name,url,canonical_url,source_type,source_tier) VALUES ('us','https://us/','https://us/','regulator',4)`)
	usSrc, _ := res.LastInsertId()
	res, _ = db.ExecContext(ctx, `INSERT INTO crawl_jobs (source_id,country_id,job_type,status) VALUES (?,?, 'archive_crawl','pending')`, usSrc, usCid)
	usJid, _ := res.LastInsertId()

	clients := Clients{IAC: &fixtureClient{Records: []RegionalRecord{{Ref: "X1", Title: "A", OriginalURL: "https://mak.aero/1"}}}}
	processed, err := ProcessPending(ctx, db, clients, 0, "")
	if err != nil {
		t.Fatal(err)
	}
	if processed < 1 {
		t.Fatalf("processed = %d, want >= 1", processed)
	}
	var stale, us string
	db.QueryRowContext(ctx, `SELECT status FROM crawl_jobs WHERE id=?`, staleJid).Scan(&stale)
	db.QueryRowContext(ctx, `SELECT status FROM crawl_jobs WHERE id=?`, usJid).Scan(&us)
	if stale == "running" {
		t.Error("stale regional_raio job should have been re-processed")
	}
	if us != "pending" {
		t.Errorf("non-regional (direct_public_archive) job must be untouched, got %q", us)
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

// TestRunJobZeroFoundEmitsSilentFailTripwire pins GO-CP-4: a job that
// completes cleanly (no client error) but finds zero records must NOT
// silently finalize as an indistinguishable 'success' — it must go
// 'partial', carry a stats_json warning, and print the grep-able
// SILENT_FAIL_SUSPECT token.
func TestRunJobZeroFoundEmitsSilentFailTripwire(t *testing.T) {
	ctx, db := seededRegionalDB(t)
	cid, jid := insertRegionalJob(t, ctx, db, "RU", "running", 0)
	clients := Clients{IAC: &fixtureClient{Records: nil}}

	out := captureStderr(t, func() {
		if err := RunJob(ctx, db, clients, Job{ID: jid, CountryID: cid, ISO2: "RU", BodyCode: "IAC"}); err != nil {
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

// TestRunJobZeroFoundRegressionNotesPriorRun pins the "bonus" comparison
// against the previous run for the same country/job_type: when the prior run
// found records but this one finds none, the message and warning must call
// out the 100%→0 drop distinctly from a merely-quiet body.
func TestRunJobZeroFoundRegressionNotesPriorRun(t *testing.T) {
	ctx, db := seededRegionalDB(t)
	cid, jid1 := insertRegionalJob(t, ctx, db, "RU", "running", 0)
	clientsWithData := Clients{IAC: &fixtureClient{Records: []RegionalRecord{
		{Ref: "2024-RA-01", Title: "A", OriginalURL: "https://mak.aero/1"},
	}}}
	if err := RunJob(ctx, db, clientsWithData, Job{ID: jid1, CountryID: cid, ISO2: "RU", BodyCode: "IAC"}); err != nil {
		t.Fatal(err)
	}

	// Second job, same country, this run finds nothing.
	res, err := db.ExecContext(ctx, `INSERT INTO crawl_jobs (source_id,country_id,job_type,status)
		SELECT source_id, country_id, job_type, 'running' FROM crawl_jobs WHERE id=?`, jid1)
	if err != nil {
		t.Fatal(err)
	}
	jid2, _ := res.LastInsertId()

	clientsEmpty := Clients{IAC: &fixtureClient{Records: nil}}
	out := captureStderr(t, func() {
		if err := RunJob(ctx, db, clientsEmpty, Job{ID: jid2, CountryID: cid, ISO2: "RU", BodyCode: "IAC"}); err != nil {
			t.Fatal(err)
		}
	})

	if !strings.Contains(out, "prev_found=1") || !strings.Contains(out, "regression=100pct_to_0") {
		t.Fatalf("expected a prior-run regression note, got: %q", out)
	}
	var stats string
	db.QueryRowContext(ctx, `SELECT stats_json FROM crawl_jobs WHERE id=?`, jid2).Scan(&stats)
	var s struct{ Warning string }
	json.Unmarshal([]byte(stats), &s)
	if s.Warning != "found_zero_regression" {
		t.Fatalf("Warning = %q, want found_zero_regression", s.Warning)
	}
}

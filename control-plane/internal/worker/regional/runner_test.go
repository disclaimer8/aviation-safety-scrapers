package regional

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
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

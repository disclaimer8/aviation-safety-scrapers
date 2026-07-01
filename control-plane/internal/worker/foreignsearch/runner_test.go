package foreignsearch

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"testing"
)

func insertForeignJob(t *testing.T, ctx context.Context, db *sql.DB, iso2, jobType string, priority float64, status string, startedAgoMs int64) (int64, int64) {
	t.Helper()
	if _, err := db.ExecContext(ctx, `
		INSERT INTO countries (iso2, iso3, name, region, policy_status, coverage_status, coverage_score, effort_score, priority_score)
		VALUES (?, ?, 'N','R','allowed','delegated_to_foreign_authority',3,2, ?)`, iso2, iso2+"X", priority); err != nil {
		t.Fatal(err)
	}
	var cid int64
	db.QueryRowContext(ctx, `SELECT id FROM countries WHERE iso2=?`, iso2).Scan(&cid)
	res, _ := db.ExecContext(ctx, `INSERT INTO sources (name,url,canonical_url,source_type,source_tier)
		VALUES (?, ?, ?, 'official_foreign_accredited_rep',2)`, "s"+iso2, "https://s/"+iso2, "https://s/"+iso2)
	srcID, _ := res.LastInsertId()
	res, err := db.ExecContext(ctx, `INSERT INTO crawl_jobs (source_id,country_id,job_type,status) VALUES (?,?,?,?)`, srcID, cid, jobType, status)
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
	ctx, db := foreignTestDB(t)
	_, jid := insertForeignJob(t, ctx, db, "BS", "ntsb_foreign_search", 50, "running", 0)
	var cid int64
	db.QueryRowContext(ctx, `SELECT country_id FROM crawl_jobs WHERE id=?`, jid).Scan(&cid)
	clients := Clients{NTSB: &fixtureClient{Records: []ForeignRecord{
		{ForeignRef: "CEN20LA001", Title: "A", OriginalURL: "https://ntsb/1"},
		{ForeignRef: "CEN20LA002", Title: "B", OriginalURL: "https://ntsb/2"},
	}}}
	if err := RunJob(ctx, db, clients, Job{ID: jid, CountryID: cid, ISO2: "BS", JobType: "ntsb_foreign_search"}); err != nil {
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

// TestRunJobBEAStagesCountryLess is the GO-CP-1 regression test: BEA's
// notified-events listing is body-wide (not filtered per country — see
// bea.go's Search doc comment), so RunJob must stage its records WITHOUT the
// job's own country, unlike NTSB (which IS genuinely filtered per country via
// the CAROL Country query param and keeps the job's country as before).
func TestRunJobBEAStagesCountryLess(t *testing.T) {
	ctx, db := foreignTestDB(t)
	_, jid := insertForeignJob(t, ctx, db, "FR", "bea_foreign_search", 50, "running", 0)
	var cid int64
	db.QueryRowContext(ctx, `SELECT country_id FROM crawl_jobs WHERE id=?`, jid).Scan(&cid)
	clients := Clients{BEA: &fixtureClient{Records: []ForeignRecord{
		{ForeignRef: "bea-2026-001", Title: "Accident on 01/01/2026", OriginalURL: "https://bea.aero/1"},
	}}}
	if err := RunJob(ctx, db, clients, Job{ID: jid, CountryID: cid, ISO2: "FR", JobType: "bea_foreign_search"}); err != nil {
		t.Fatal(err)
	}
	var countryID sql.NullInt64
	if err := db.QueryRowContext(ctx,
		`SELECT country_id FROM staged_foreign_documents WHERE foreign_ref='bea-2026-001'`).Scan(&countryID); err != nil {
		t.Fatal(err)
	}
	if countryID.Valid {
		t.Fatalf("country_id = %v, want NULL — BEA's listing is body-wide and must not inherit the job's country (FR)", countryID)
	}
}

// TestRunJobNTSBStagesWithCountry pins the unchanged NTSB behavior alongside
// the BEA fix above: NTSB genuinely queries CAROL scoped to one country, so
// its staged records keep the job's country.
func TestRunJobNTSBStagesWithCountry(t *testing.T) {
	ctx, db := foreignTestDB(t)
	_, jid := insertForeignJob(t, ctx, db, "BS", "ntsb_foreign_search", 50, "running", 0)
	var cid int64
	db.QueryRowContext(ctx, `SELECT country_id FROM crawl_jobs WHERE id=?`, jid).Scan(&cid)
	clients := Clients{NTSB: &fixtureClient{Records: []ForeignRecord{
		{ForeignRef: "CEN20LA099", Title: "A", OriginalURL: "https://ntsb/99"},
	}}}
	if err := RunJob(ctx, db, clients, Job{ID: jid, CountryID: cid, ISO2: "BS", JobType: "ntsb_foreign_search"}); err != nil {
		t.Fatal(err)
	}
	var countryID sql.NullInt64
	if err := db.QueryRowContext(ctx,
		`SELECT country_id FROM staged_foreign_documents WHERE foreign_ref='CEN20LA099'`).Scan(&countryID); err != nil {
		t.Fatal(err)
	}
	if !countryID.Valid || countryID.Int64 != cid {
		t.Fatalf("country_id = %v, want %d (NTSB is genuinely per-country and must keep the job's country)", countryID, cid)
	}
}

func TestRunJobSearchErrorFails(t *testing.T) {
	ctx, db := foreignTestDB(t)
	_, jid := insertForeignJob(t, ctx, db, "BS", "ntsb_foreign_search", 50, "running", 0)
	var cid int64
	db.QueryRowContext(ctx, `SELECT country_id FROM crawl_jobs WHERE id=?`, jid).Scan(&cid)
	clients := Clients{NTSB: &fixtureClient{Err: errors.New("boom")}}
	if err := RunJob(ctx, db, clients, Job{ID: jid, CountryID: cid, ISO2: "BS", JobType: "ntsb_foreign_search"}); err != nil {
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

func TestProcessPendingResumesStaleRunning(t *testing.T) {
	ctx, db := foreignTestDB(t)
	// stale running (2h old) + fresh running (now)
	_, staleJid := insertForeignJob(t, ctx, db, "ST", "ntsb_foreign_search", 100, "running", 7200000)
	_, freshJid := insertForeignJob(t, ctx, db, "FR", "ntsb_foreign_search", 10, "running", 1000)
	clients := Clients{NTSB: &fixtureClient{Records: []ForeignRecord{{ForeignRef: "X1", Title: "A", OriginalURL: "https://n/1"}}}}
	processed, err := ProcessPending(ctx, db, clients, 0)
	if err != nil {
		t.Fatal(err)
	}
	if processed < 1 {
		t.Fatalf("processed = %d, want >= 1 (stale running re-picked)", processed)
	}
	var stale, fresh string
	db.QueryRowContext(ctx, `SELECT status FROM crawl_jobs WHERE id=?`, staleJid).Scan(&stale)
	db.QueryRowContext(ctx, `SELECT status FROM crawl_jobs WHERE id=?`, freshJid).Scan(&fresh)
	if stale == "running" {
		t.Error("stale running job should have been re-processed (not still running)")
	}
	if fresh != "running" {
		t.Errorf("fresh running job should be untouched, got %q", fresh)
	}
}

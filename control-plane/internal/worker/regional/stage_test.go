package regional

import (
	"context"
	"database/sql"
	"testing"
)

func regionalStageFixtureJob(t *testing.T, ctx context.Context, db *sql.DB) (int64, int64) {
	t.Helper()
	if _, err := db.ExecContext(ctx, `
		INSERT INTO countries (iso2, iso3, name, region, policy_status, coverage_status, coverage_score, effort_score)
		VALUES ('XR','XRR','Test R','Test','allowed','regional_raio',2,3)`); err != nil {
		t.Fatal(err)
	}
	var cid int64
	db.QueryRowContext(ctx, `SELECT id FROM countries WHERE iso2='XR'`).Scan(&cid)
	res, err := db.ExecContext(ctx, `INSERT INTO sources (name,url,canonical_url,source_type,source_tier)
		VALUES ('rg','https://rg/','https://rg/','regional_body',4)`)
	if err != nil {
		t.Fatal(err)
	}
	srcID, _ := res.LastInsertId()
	res, err = db.ExecContext(ctx, `INSERT INTO crawl_jobs (source_id,country_id,job_type,status)
		VALUES (?,?, 'archive_crawl','running')`, srcID, cid)
	if err != nil {
		t.Fatal(err)
	}
	jid, _ := res.LastInsertId()
	return cid, jid
}

func TestStageRecordsDedups(t *testing.T) {
	ctx, db := seededRegionalDB(t)
	cid, jid := regionalStageFixtureJob(t, ctx, db)
	recs := []RegionalRecord{
		{Ref: "2024-RA-01", Title: "A", OriginalURL: "https://mak.aero/1", ReportURL: "https://mak.aero/1.pdf", Mimetype: "application/pdf", OccurrenceDate: "2024-01-02"},
		{Ref: "2024-RA-02", Title: "B", OriginalURL: "https://mak.aero/2"},
	}
	n, err := StageRecords(ctx, db, jid, cid, "IAC", recs)
	if err != nil {
		t.Fatal(err)
	}
	if n != 2 {
		t.Fatalf("staged = %d, want 2", n)
	}
	n2, _ := StageRecords(ctx, db, jid, cid, "IAC", recs)
	if n2 != 0 {
		t.Fatalf("re-stage = %d, want 0", n2)
	}
	var total int
	db.QueryRowContext(ctx, `SELECT COUNT(*) FROM staged_regional_documents`).Scan(&total)
	if total != 2 {
		t.Fatalf("total = %d, want 2", total)
	}
}

// TestStageRecordsCountryLessWhenCallerPassesZero is the GO-CP-1 regression
// test: a body-wide source (every regional body currently wired) must be able
// to stage a record with NO country claim rather than being forced to stamp
// the crawling job's own country. RunJob always passes 0 for this reason; this
// test drives StageRecords directly to pin the contract at the function level.
func TestStageRecordsCountryLessWhenCallerPassesZero(t *testing.T) {
	ctx, db := seededRegionalDB(t)
	_, jid := regionalStageFixtureJob(t, ctx, db)
	recs := []RegionalRecord{
		{Ref: "2026-EW-307SL", Title: "An-2 EW-307SL, Ozertso", OriginalURL: "https://mak-iac.org/1"},
	}
	n, err := StageRecords(ctx, db, jid, 0, "IAC", recs)
	if err != nil {
		t.Fatal(err)
	}
	if n != 1 {
		t.Fatalf("staged = %d, want 1", n)
	}
	var countryID sql.NullInt64
	if err := db.QueryRowContext(ctx,
		`SELECT country_id FROM staged_regional_documents WHERE ref='2026-EW-307SL'`).Scan(&countryID); err != nil {
		t.Fatal(err)
	}
	if countryID.Valid {
		t.Fatalf("country_id = %v, want NULL (no country claim for a body-wide record)", countryID)
	}
}

// TestStageRecordsPrefersDeterministicPerRecordCountry verifies part (c) of the
// GO-CP-1 fix: when a RegionalRecord carries its own CountryISO2 (a future
// parser reading a per-record field from the listing), that value is used
// instead of the caller's fallback country — even when the fallback is a
// different, real country.
func TestStageRecordsPrefersDeterministicPerRecordCountry(t *testing.T) {
	ctx, db := seededRegionalDB(t)
	ruCID, jid := regionalStageFixtureJobFor(t, ctx, db, "RU")
	recs := []RegionalRecord{
		// The job ran under RU, but the record itself is deterministically
		// known (e.g. a future ECCAA/BAGAIA member-state column) to be BY.
		{Ref: "2026-BY-01", Title: "An-2 EW-307SL", OriginalURL: "https://mak-iac.org/2", CountryISO2: "BY"},
	}
	if _, err := StageRecords(ctx, db, jid, ruCID, "IAC", recs); err != nil {
		t.Fatal(err)
	}
	var gotISO2 string
	if err := db.QueryRowContext(ctx, `
		SELECT c.iso2 FROM staged_regional_documents d JOIN countries c ON c.id = d.country_id
		 WHERE d.ref='2026-BY-01'`).Scan(&gotISO2); err != nil {
		t.Fatal(err)
	}
	if gotISO2 != "BY" {
		t.Fatalf("country_id resolved to iso2=%q, want BY (deterministic per-record value must win over job country RU)", gotISO2)
	}
}

// TestStageRecordsUnknownDeterministicISO2FallsBack verifies that a garbage or
// unmapped CountryISO2 does not abort staging — it falls back to the caller's
// countryID (or NULL, when that is also <=0) rather than erroring.
func TestStageRecordsUnknownDeterministicISO2FallsBack(t *testing.T) {
	ctx, db := seededRegionalDB(t)
	_, jid := regionalStageFixtureJob(t, ctx, db)
	recs := []RegionalRecord{
		{Ref: "2026-ZZ-01", Title: "Unmappable", OriginalURL: "https://mak-iac.org/3", CountryISO2: "ZZ"},
	}
	n, err := StageRecords(ctx, db, jid, 0, "IAC", recs)
	if err != nil {
		t.Fatal(err)
	}
	if n != 1 {
		t.Fatalf("staged = %d, want 1", n)
	}
	var countryID sql.NullInt64
	if err := db.QueryRowContext(ctx,
		`SELECT country_id FROM staged_regional_documents WHERE ref='2026-ZZ-01'`).Scan(&countryID); err != nil {
		t.Fatal(err)
	}
	if countryID.Valid {
		t.Fatalf("country_id = %v, want NULL (unknown ISO2 %q must not abort or guess)", countryID, "ZZ")
	}
}

// regionalStageFixtureJobFor is like regionalStageFixtureJob but uses a real,
// already-seeded country (from seededRegionalDB) instead of inserting a
// synthetic 'XR' one, so the test can assert on a real distinct ISO2.
func regionalStageFixtureJobFor(t *testing.T, ctx context.Context, db *sql.DB, iso2 string) (int64, int64) {
	t.Helper()
	cid := countryID(t, ctx, db, iso2)
	res, err := db.ExecContext(ctx, `INSERT OR IGNORE INTO sources (name,url,canonical_url,source_type,source_tier)
		VALUES ('rg2','https://rg2/','https://rg2/','regional_body',4)`)
	if err != nil {
		t.Fatal(err)
	}
	_ = res
	var srcID int64
	if err := db.QueryRowContext(ctx, `SELECT id FROM sources WHERE canonical_url='https://rg2/'`).Scan(&srcID); err != nil {
		t.Fatal(err)
	}
	res, err = db.ExecContext(ctx, `INSERT INTO crawl_jobs (source_id,country_id,job_type,status)
		VALUES (?,?, 'archive_crawl','running')`, srcID, cid)
	if err != nil {
		t.Fatal(err)
	}
	jid, _ := res.LastInsertId()
	return cid, jid
}

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

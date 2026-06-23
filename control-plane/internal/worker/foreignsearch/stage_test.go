package foreignsearch

import (
	"context"
	"database/sql"
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/database"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/migrations"
)

func foreignTestDB(t *testing.T) (context.Context, *sql.DB) {
	t.Helper()
	ctx := context.Background()
	db, err := database.Open(t.TempDir() + "/coverage.db")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if err := migrations.Apply(ctx, db); err != nil {
		t.Fatal(err)
	}
	return ctx, db
}

func stageFixtureJob(t *testing.T, ctx context.Context, db *sql.DB) (int64, int64) {
	t.Helper()
	if _, err := db.ExecContext(ctx, `
		INSERT INTO countries (iso2, iso3, name, region, policy_status, coverage_status, coverage_score, effort_score)
		VALUES ('XF','XFF','Test F','Test','allowed','delegated_to_foreign_authority',3,2)`); err != nil {
		t.Fatal(err)
	}
	var cid int64
	db.QueryRowContext(ctx, `SELECT id FROM countries WHERE iso2='XF'`).Scan(&cid)
	res, err := db.ExecContext(ctx, `INSERT INTO sources (name,url,canonical_url,source_type,source_tier)
		VALUES ('fs','https://fs/','https://fs/','official_foreign_accredited_rep',2)`)
	if err != nil {
		t.Fatal(err)
	}
	srcID, _ := res.LastInsertId()
	res, err = db.ExecContext(ctx, `INSERT INTO crawl_jobs (source_id,country_id,job_type,status)
		VALUES (?,?, 'ntsb_foreign_search','running')`, srcID, cid)
	if err != nil {
		t.Fatal(err)
	}
	jid, _ := res.LastInsertId()
	return cid, jid
}

func TestStageRecordsDedups(t *testing.T) {
	ctx, db := foreignTestDB(t)
	cid, jid := stageFixtureJob(t, ctx, db)

	recs := []ForeignRecord{
		{ForeignRef: "CEN20LA001", Title: "A", OriginalURL: "https://ntsb/1", ReportURL: "https://ntsb/1.pdf", Mimetype: "application/pdf", OccurrenceDate: "2020-01-02"},
		{ForeignRef: "CEN20LA002", Title: "B", OriginalURL: "https://ntsb/2"},
	}
	n, err := StageRecords(ctx, db, jid, cid, "ntsb", recs)
	if err != nil {
		t.Fatal(err)
	}
	if n != 2 {
		t.Fatalf("staged = %d, want 2", n)
	}
	n2, err := StageRecords(ctx, db, jid, cid, "ntsb", recs)
	if err != nil {
		t.Fatal(err)
	}
	if n2 != 0 {
		t.Fatalf("re-stage = %d, want 0 (dedup)", n2)
	}
	var total int
	db.QueryRowContext(ctx, `SELECT COUNT(*) FROM staged_foreign_documents`).Scan(&total)
	if total != 2 {
		t.Fatalf("total = %d, want 2", total)
	}
}

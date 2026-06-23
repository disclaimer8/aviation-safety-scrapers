package migrations

import (
	"context"
	"testing"
)

func TestMigration007RegionalSchema(t *testing.T) {
	db := applyTestSchema(t)
	ctx := context.Background()
	if _, err := db.ExecContext(ctx, `
		INSERT INTO countries (iso2, iso3, name, region, policy_status, coverage_status, coverage_score, effort_score)
		VALUES ('XR','XRR','Test R','Test','allowed','regional_raio',2,3)`); err != nil {
		t.Fatal(err)
	}
	var cid int64
	db.QueryRowContext(ctx, `SELECT id FROM countries WHERE iso2='XR'`).Scan(&cid)
	res, err := db.ExecContext(ctx, `INSERT INTO sources (name,url,canonical_url,source_type,source_tier)
		VALUES ('t','https://t/','https://t/','regional_body',4)`)
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
	ins := `INSERT INTO staged_regional_documents (crawl_job_id, country_id, body_code, ref, title, original_url)
		VALUES (?,?,?,?,?,?)`
	if _, err := db.ExecContext(ctx, ins, jid, cid, "IAC", "2024-RA-01", "Crash A", "https://mak.aero/x"); err != nil {
		t.Fatalf("insert staged regional doc: %v", err)
	}
	if _, err := db.ExecContext(ctx, ins, jid, cid, "IAC", "2024-RA-01", "dup", "https://mak.aero/y"); err == nil {
		t.Fatal("expected UNIQUE(body_code, ref) violation")
	}
	if _, err := db.ExecContext(ctx, ins, jid, cid, "XXX", "r2", "t", "https://x"); err == nil {
		t.Fatal("expected body_code CHECK violation for 'XXX'")
	}
}

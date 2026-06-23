package migrations

import (
	"context"
	"testing"
)

func TestMigration008ForeignSchema(t *testing.T) {
	db := applyTestSchema(t)
	ctx := context.Background()

	// Need a country + source + crawl_job to satisfy FKs.
	if _, err := db.ExecContext(ctx, `
		INSERT INTO countries (iso2, iso3, name, region, policy_status, coverage_status, coverage_score, effort_score)
		VALUES ('XF','XFF','Test F','Test','allowed','delegated_to_foreign_authority',3,2)`); err != nil {
		t.Fatal(err)
	}
	var cid int64
	db.QueryRowContext(ctx, `SELECT id FROM countries WHERE iso2='XF'`).Scan(&cid)
	res, err := db.ExecContext(ctx, `INSERT INTO sources (name,url,canonical_url,source_type,source_tier)
		VALUES ('t','https://t/','https://t/','official_foreign_accredited_rep',2)`)
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

	ins := `INSERT INTO staged_foreign_documents
		(crawl_job_id, country_id, authority, foreign_ref, title, original_url)
		VALUES (?,?,?,?,?,?)`
	if _, err := db.ExecContext(ctx, ins, jid, cid, "ntsb", "CEN20LA001", "Accident A", "https://ntsb/CEN20LA001"); err != nil {
		t.Fatalf("insert staged foreign doc: %v", err)
	}
	// Same (authority, foreign_ref) must conflict.
	if _, err := db.ExecContext(ctx, ins, jid, cid, "ntsb", "CEN20LA001", "dup", "https://ntsb/x"); err == nil {
		t.Fatal("expected UNIQUE(authority, foreign_ref) violation")
	}
	// A bad authority value must violate the CHECK.
	if _, err := db.ExecContext(ctx, ins, jid, cid, "faa", "X1", "t", "https://x"); err == nil {
		t.Fatal("expected authority CHECK violation for 'faa'")
	}
}

package wayback

import (
	"context"
	"database/sql"
	"testing"
)

// stageFixtureJob inserts a country + source + running crawl_job and returns
// (countryID, jobID) for staging/download tests.
func stageFixtureJob(t *testing.T, ctx context.Context, db *sql.DB) (int64, int64) {
	t.Helper()
	cid := insertCountry(t, ctx, db, "ZZ", nil)
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
	return cid, jid
}

func TestStageSnapshotsDedupsByDigest(t *testing.T) {
	ctx, db := waybackTestDB(t)
	cid, jid := stageFixtureJob(t, ctx, db)

	snaps := []Snapshot{
		{OriginalURL: "http://x/a.pdf", ArchivedURL: "https://web.archive.org/web/2010id_/http://x/a.pdf", Timestamp: "2010", Mimetype: "application/pdf", Digest: "D1", Length: 10},
		{OriginalURL: "http://x/b.pdf", ArchivedURL: "https://web.archive.org/web/2011id_/http://x/b.pdf", Timestamp: "2011", Mimetype: "application/pdf", Digest: "D2", Length: 20},
	}
	staged, err := StageSnapshots(ctx, db, jid, cid, snaps)
	if err != nil {
		t.Fatal(err)
	}
	if staged != 2 {
		t.Fatalf("staged = %d, want 2", staged)
	}

	// Re-staging the same digests inserts nothing.
	staged2, err := StageSnapshots(ctx, db, jid, cid, snaps)
	if err != nil {
		t.Fatal(err)
	}
	if staged2 != 0 {
		t.Fatalf("re-stage = %d, want 0 (dedup)", staged2)
	}

	var total int
	if err := db.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM staged_wayback_documents WHERE country_id=?`, cid).Scan(&total); err != nil {
		t.Fatal(err)
	}
	if total != 2 {
		t.Fatalf("total staged = %d, want 2", total)
	}
}

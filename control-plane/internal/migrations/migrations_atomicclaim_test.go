package migrations

import (
	"context"
	"database/sql"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"testing/fstest"
)

// preMigration012FS returns a fs.FS containing every embedded migration file
// except 012_atomic_claim.sql itself, built from the real files on disk (not
// duplicated inline) so it always tracks 001..011 exactly. Used to reproduce
// a live minipc DB that predates the GO-CP-9 atomic-claim invariant.
func preMigration012FS(t *testing.T) fstest.MapFS {
	t.Helper()
	entries, err := os.ReadDir("sql")
	if err != nil {
		t.Fatal(err)
	}
	out := fstest.MapFS{}
	for _, e := range entries {
		if e.IsDir() || strings.HasPrefix(e.Name(), "012_") {
			continue
		}
		body, err := os.ReadFile(filepath.Join("sql", e.Name()))
		if err != nil {
			t.Fatal(err)
		}
		out["sql/"+e.Name()] = &fstest.MapFile{Data: body}
	}
	return out
}

// TestMigration012ResolvesPreexistingDuplicatesBeforeUniqueIndex pins GO-CP-9's
// migration-safety requirement: live minipc DBs predate the atomic-claim
// invariant and may already contain duplicate (country_id, job_type) rows
// among pending/running crawl_jobs — exactly the shape the old non-atomic
// planner Enqueue and worker claim UPDATE could produce. Migration 012 must
// not brick such a DB by failing outright on the first duplicate; it must
// deterministically resolve duplicates (keep the newest by id, mark the rest
// 'failed' with an explanatory error) BEFORE creating the partial unique
// index, and the index must then be live and enforced.
func TestMigration012ResolvesPreexistingDuplicatesBeforeUniqueIndex(t *testing.T) {
	ctx := context.Background()
	db := openTestDB(t)

	if err := applyFS(ctx, db, preMigration012FS(t)); err != nil {
		t.Fatalf("apply pre-012 schema (001..011): %v", err)
	}

	// Seed a country + source to satisfy crawl_jobs' FKs.
	if _, err := db.ExecContext(ctx, `
		INSERT INTO countries
			(iso2, iso3, name, region, policy_status, coverage_status,
			 coverage_score, effort_score)
		VALUES ('DZ','DZZ','Dupeland','Test','allowed','no_public_archive',1,3)
	`); err != nil {
		t.Fatal(err)
	}
	var countryID int64
	if err := db.QueryRowContext(ctx, `SELECT id FROM countries WHERE iso2='DZ'`).Scan(&countryID); err != nil {
		t.Fatal(err)
	}
	res, err := db.ExecContext(ctx, `
		INSERT INTO sources (name, url, canonical_url, source_type, source_tier)
		VALUES ('dup-src','https://dup/','https://dup/','wayback',5)`)
	if err != nil {
		t.Fatal(err)
	}
	srcID, _ := res.LastInsertId()

	insertJob := func(status string) int64 {
		res, err := db.ExecContext(ctx, `
			INSERT INTO crawl_jobs (source_id, country_id, job_type, status)
			VALUES (?, ?, 'wayback_cdx', ?)`, srcID, countryID, status)
		if err != nil {
			t.Fatal(err)
		}
		id, _ := res.LastInsertId()
		return id
	}

	// Three pending/running rows for the SAME (country_id, job_type) — the
	// exact GO-CP-9 shape (e.g. two overlapping planner runs both inserting
	// before the unique index existed). Ascending id order = insertion order.
	oldest := insertJob("pending")
	middle := insertJob("running")
	newest := insertJob("pending")

	// A terminal-status row in the SAME group must be left alone — it is not
	// part of the pending/running uniqueness and was never a race participant.
	terminalSameGroup := insertJob("success")

	// An unrelated (different job_type) row must also be left alone.
	res, err = db.ExecContext(ctx, `
		INSERT INTO crawl_jobs (source_id, country_id, job_type, status)
		VALUES (?, ?, 'archive_crawl', 'pending')`, srcID, countryID)
	if err != nil {
		t.Fatal(err)
	}
	unrelatedID, _ := res.LastInsertId()

	// Now apply the full real migration set. 001..011 are already recorded
	// with matching checksums (skipped); only 012 actually executes.
	if err := Apply(ctx, db); err != nil {
		t.Fatalf("apply 012 must not brick a DB with pre-existing duplicate active claims: %v", err)
	}

	statusOf := func(id int64) string {
		var s string
		if err := db.QueryRowContext(ctx, `SELECT status FROM crawl_jobs WHERE id=?`, id).Scan(&s); err != nil {
			t.Fatal(err)
		}
		return s
	}
	errorOf := func(id int64) string {
		var s sql.NullString
		if err := db.QueryRowContext(ctx, `SELECT error FROM crawl_jobs WHERE id=?`, id).Scan(&s); err != nil {
			t.Fatal(err)
		}
		if !s.Valid {
			return ""
		}
		return s.String
	}

	// Newest survives as the sole pending/running row for the group.
	if got := statusOf(newest); got != "pending" {
		t.Errorf("newest job status = %q, want unchanged pending (must be the survivor)", got)
	}
	// The two older duplicates are resolved to 'failed' with an explanatory,
	// truthful error (CrawlJobStatus has no 'skipped'/'cancelled' value).
	if got := statusOf(oldest); got != "failed" {
		t.Errorf("oldest job status = %q, want failed", got)
	}
	if got := statusOf(middle); got != "failed" {
		t.Errorf("middle job status = %q, want failed", got)
	}
	for _, id := range []int64{oldest, middle} {
		e := errorOf(id)
		if !strings.Contains(e, "duplicate_claim_resolved_by_migration_012_atomic_claim") {
			t.Errorf("job %d error = %q, want it to explain the duplicate resolution", id, e)
		}
		if !strings.Contains(e, "job id") {
			t.Errorf("job %d error = %q, want it to name the surviving job", id, e)
		}
	}

	// Untouched: terminal row in the same group, and the unrelated job_type.
	if got := statusOf(terminalSameGroup); got != "success" {
		t.Errorf("terminal same-group job status = %q, want unchanged success", got)
	}
	if got := statusOf(unrelatedID); got != "pending" {
		t.Errorf("unrelated job_type job status = %q, want unchanged pending", got)
	}

	// The partial unique index now exists and is enforced: a fresh duplicate
	// active claim for the same (country_id, job_type) must be rejected.
	if _, err := db.ExecContext(ctx, `
		INSERT INTO crawl_jobs (source_id, country_id, job_type, status)
		VALUES (?, ?, 'wayback_cdx', 'pending')`, srcID, countryID); err == nil {
		t.Fatal("expected unique index violation inserting a second active wayback_cdx job for DZ")
	}
}

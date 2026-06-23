package planner

import (
	"context"
	"database/sql"
	"testing"
)

func countJobs(t *testing.T, ctx context.Context, db *sql.DB) int {
	t.Helper()
	var n int
	if err := db.QueryRowContext(ctx, `SELECT COUNT(*) FROM crawl_jobs`).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}

func TestEnqueueInsertsAndIsIdempotent(t *testing.T) {
	ctx, db := seededDB(t)
	nowMs := int64(1_700_000_000_000)

	plan, err := BuildPlan(ctx, db, nowMs, 0)
	if err != nil {
		t.Fatal(err)
	}
	inserted, err := Enqueue(ctx, db, plan)
	if err != nil {
		t.Fatal(err)
	}
	if inserted != plan.JobsPlanned || inserted == 0 {
		t.Fatalf("inserted %d, want JobsPlanned %d (>0)", inserted, plan.JobsPlanned)
	}
	if got := countJobs(t, ctx, db); got != inserted {
		t.Fatalf("crawl_jobs has %d rows, want %d", got, inserted)
	}

	// Re-plan + re-enqueue: all pairs now have an active job → zero inserts.
	plan2, err := BuildPlan(ctx, db, nowMs, 0)
	if err != nil {
		t.Fatal(err)
	}
	if plan2.JobsPlanned != 0 {
		t.Fatalf("second plan JobsPlanned = %d, want 0 (all active)", plan2.JobsPlanned)
	}
	inserted2, err := Enqueue(ctx, db, plan2)
	if err != nil {
		t.Fatal(err)
	}
	if inserted2 != 0 {
		t.Fatalf("second enqueue inserted %d, want 0", inserted2)
	}
	if got := countJobs(t, ctx, db); got != inserted {
		t.Fatalf("crawl_jobs grew to %d, want stable %d", got, inserted)
	}
}

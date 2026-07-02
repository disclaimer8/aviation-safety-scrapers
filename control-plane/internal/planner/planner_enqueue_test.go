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

// TestEnqueueConcurrentPlanDoesNotDoubleEnqueue pins GO-CP-9: BuildPlan's
// HasActive read and Enqueue's INSERT are separate, non-serialized steps, so
// two concurrent planner runs can both read the DB before either has
// inserted anything and both decide would_enqueue for the same (country_id,
// job_type) pairs. Migration 012's partial unique index on crawl_jobs
// (country_id, job_type) WHERE status IN ('pending','running'), combined with
// Enqueue's ON CONFLICT ... DO NOTHING, must make the second (racing) Enqueue
// call a silent no-op instead of a constraint-violation error that aborts the
// whole plan.
func TestEnqueueConcurrentPlanDoesNotDoubleEnqueue(t *testing.T) {
	ctx, db := seededDB(t)
	nowMs := int64(1_700_000_000_000)

	// Two BuildPlan calls against the same untouched DB state — as if two
	// planner processes raced and both read before either wrote.
	plan1, err := BuildPlan(ctx, db, nowMs, 0)
	if err != nil {
		t.Fatal(err)
	}
	plan2, err := BuildPlan(ctx, db, nowMs, 0)
	if err != nil {
		t.Fatal(err)
	}
	if plan1.JobsPlanned == 0 {
		t.Fatal("plan1.JobsPlanned = 0, fixture produced nothing to race over")
	}
	if plan2.JobsPlanned != plan1.JobsPlanned {
		t.Fatalf("plan2.JobsPlanned = %d, want %d (identical read of untouched DB)",
			plan2.JobsPlanned, plan1.JobsPlanned)
	}

	inserted1, err := Enqueue(ctx, db, plan1)
	if err != nil {
		t.Fatal(err)
	}
	if inserted1 != plan1.JobsPlanned {
		t.Fatalf("inserted1 = %d, want %d", inserted1, plan1.JobsPlanned)
	}

	// plan2 is now stale: every pair it planned already has an active job
	// inserted by plan1. This must be a graceful no-op, not an error.
	inserted2, err := Enqueue(ctx, db, plan2)
	if err != nil {
		t.Fatalf("Enqueue on a stale/racing plan must not error: %v", err)
	}
	if inserted2 != 0 {
		t.Fatalf("inserted2 = %d, want 0 (every pair already claimed by plan1)", inserted2)
	}

	if got := countJobs(t, ctx, db); got != inserted1 {
		t.Fatalf("crawl_jobs has %d rows, want %d (racing plan2 must not create duplicates)",
			got, inserted1)
	}
}

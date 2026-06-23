package planner

import "testing"

func TestBuildPlanProducesDecisions(t *testing.T) {
	ctx, db := seededDB(t)
	nowMs := int64(1_700_000_000_000)

	plan, err := BuildPlan(ctx, db, nowMs, 0)
	if err != nil {
		t.Fatal(err)
	}
	if plan.CandidateCountries == 0 || len(plan.Jobs) == 0 {
		t.Fatalf("empty plan: %+v", plan)
	}
	// All decisions on a fresh DB are would_enqueue (no existing jobs).
	for _, j := range plan.Jobs {
		if j.Decision != DecisionWouldEnqueue {
			t.Fatalf("%s/%s decision = %q, want would_enqueue", j.ISO2, j.JobType, j.Decision)
		}
		if j.SourceID <= 0 {
			t.Fatalf("%s/%s has no source id", j.ISO2, j.JobType)
		}
	}
	if plan.JobsPlanned != len(plan.Jobs) {
		t.Fatalf("JobsPlanned %d != len(Jobs) %d on fresh DB", plan.JobsPlanned, len(plan.Jobs))
	}

	// NG (regional_raio) must yield archive_crawl + wayback_cdx.
	var ngTypes []string
	for _, j := range plan.Jobs {
		if j.ISO2 == "NG" {
			ngTypes = append(ngTypes, string(j.JobType))
		}
	}
	if len(ngTypes) != 2 {
		t.Fatalf("NG job types = %v, want 2 (archive_crawl, wayback_cdx)", ngTypes)
	}
}

func TestBuildPlanRespectsLimit(t *testing.T) {
	ctx, db := seededDB(t)
	plan, err := BuildPlan(ctx, db, int64(1_700_000_000_000), 3)
	if err != nil {
		t.Fatal(err)
	}
	seen := map[string]bool{}
	for _, j := range plan.Jobs {
		seen[j.ISO2] = true
	}
	if len(seen) > 3 {
		t.Fatalf("limit 3 but %d countries in plan", len(seen))
	}
}

package app

import (
	"bytes"
	"context"
	"encoding/json"
	"strings"
	"testing"
)

// prepDB builds a migrated+seeded DB and returns its path.
func prepDB(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	path := dir + "/coverage.db"
	ctx := context.Background()
	var out, errb bytes.Buffer
	if code := Run(ctx, []string{"migrate", "--db", path}, &out, &errb); code != 0 {
		t.Fatalf("migrate exit %d: %s", code, errb.String())
	}
	if code := Run(ctx, []string{"seed", "--db", path}, &out, &errb); code != 0 {
		t.Fatalf("seed exit %d: %s", code, errb.String())
	}
	return path
}

func TestPlanDryRunPrintsJSON(t *testing.T) {
	path := prepDB(t)
	var out, errb bytes.Buffer
	code := Run(context.Background(),
		[]string{"plan", "--db", path, "--generated-at", "2026-06-23T12:00:00Z"},
		&out, &errb)
	if code != 0 {
		t.Fatalf("plan exit %d: %s", code, errb.String())
	}
	var plan struct {
		CandidateCountries int `json:"candidate_countries"`
		JobsPlanned        int `json:"jobs_planned"`
		Jobs               []struct {
			ISO2     string `json:"iso2"`
			JobType  string `json:"job_type"`
			Decision string `json:"decision"`
		} `json:"jobs"`
	}
	if err := json.Unmarshal(out.Bytes(), &plan); err != nil {
		t.Fatalf("output not JSON: %v\n%s", err, out.String())
	}
	if plan.CandidateCountries == 0 || len(plan.Jobs) == 0 {
		t.Fatalf("empty plan: %+v", plan)
	}
}

func TestPlanEnqueueWritesJobs(t *testing.T) {
	path := prepDB(t)
	var out, errb bytes.Buffer
	code := Run(context.Background(),
		[]string{"plan", "--db", path, "--enqueue"}, &out, &errb)
	if code != 0 {
		t.Fatalf("plan --enqueue exit %d: %s", code, errb.String())
	}
	if !strings.Contains(errb.String(), "enqueued") {
		t.Fatalf("stderr missing summary: %q", errb.String())
	}
}

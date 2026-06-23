package planner

import (
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/model"
)

func TestCadenceDurationMs(t *testing.T) {
	day := int64(24 * 60 * 60 * 1000)
	cases := map[string]int64{
		"weekly": 7 * day, "biweekly": 14 * day, "monthly": 30 * day,
		"quarterly": 90 * day, "": 90 * day, "garbage": 90 * day,
	}
	for c, want := range cases {
		if got := cadenceDurationMs(c); got != want {
			t.Errorf("cadenceDurationMs(%q) = %d, want %d", c, got, want)
		}
	}
}

func TestCadenceElapsed(t *testing.T) {
	day := int64(24 * 60 * 60 * 1000)
	now := int64(1_000_000_000_000)
	if cadenceElapsed(now, now-10*day, "quarterly") {
		t.Error("10 days < quarterly: should NOT be elapsed")
	}
	if !cadenceElapsed(now, now-100*day, "quarterly") {
		t.Error("100 days > quarterly: should be elapsed")
	}
	if !cadenceElapsed(now, now-90*day, "quarterly") {
		t.Error("exactly 90 days = quarterly: should be elapsed")
	}
}

func TestJobStateFor(t *testing.T) {
	ctx, db := seededDB(t)
	// Use a real country + source so FKs hold.
	var countryID, sourceID int64
	if err := db.QueryRowContext(ctx, `SELECT id FROM countries WHERE iso2='NG'`).Scan(&countryID); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRowContext(ctx, `SELECT id FROM sources WHERE name='Wayback Machine CDX (method)'`).Scan(&sourceID); err != nil {
		t.Fatal(err)
	}

	// No jobs yet → no active, no terminal.
	st, err := JobStateFor(ctx, db, countryID, model.CrawlJobTypeWaybackCDX)
	if err != nil {
		t.Fatal(err)
	}
	if st.HasActive || st.HasTerminal {
		t.Fatalf("fresh state = %+v, want empty", st)
	}

	// Insert a pending job → active.
	if _, err := db.ExecContext(ctx, `
		INSERT INTO crawl_jobs (source_id, country_id, job_type, status)
		VALUES (?,?, 'wayback_cdx', 'pending')`, sourceID, countryID); err != nil {
		t.Fatal(err)
	}
	st, err = JobStateFor(ctx, db, countryID, model.CrawlJobTypeWaybackCDX)
	if err != nil {
		t.Fatal(err)
	}
	if !st.HasActive {
		t.Fatal("expected HasActive after inserting pending job")
	}

	// A terminal (success) job populates HasTerminal and LastFinishedAtMs.
	if _, err := db.ExecContext(ctx, `
		INSERT INTO crawl_jobs (source_id, country_id, job_type, status, finished_at)
		VALUES (?,?, 'atsb_search', 'success', 1700000000000)`, sourceID, countryID); err != nil {
		t.Fatal(err)
	}
	ts, err := JobStateFor(ctx, db, countryID, model.CrawlJobTypeATSBSearch)
	if err != nil {
		t.Fatal(err)
	}
	if !ts.HasTerminal {
		t.Fatal("expected HasTerminal after inserting a success job")
	}
	if ts.LastFinishedAtMs != 1700000000000 {
		t.Fatalf("LastFinishedAtMs = %d, want 1700000000000", ts.LastFinishedAtMs)
	}
	if ts.HasActive {
		t.Fatal("a success-only job_type should not be HasActive")
	}
}

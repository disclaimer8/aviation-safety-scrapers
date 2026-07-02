package foreignsearch

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
)

type Job struct {
	ID        int64
	CountryID int64
	ISO2      string
	JobType   string
}

// Clients holds one AuthorityClient per foreign authority.
type Clients struct {
	NTSB AuthorityClient
	BEA  AuthorityClient
	ATSB AuthorityClient
}

type jobStats struct {
	Found  int `json:"found"`
	Staged int `json:"staged"`
	Errors int `json:"errors"`
	// Warning records a GO-CP-4 silent-fail-suspect note when found==0 for a
	// completed (non-error) job. Empty in the normal case.
	Warning string `json:"warning,omitempty"`
}

// clientFor returns the client + authority code for a job type.
func clientFor(c Clients, jobType string) (AuthorityClient, string, bool) {
	switch jobType {
	case "ntsb_foreign_search":
		return c.NTSB, "ntsb", c.NTSB != nil
	case "bea_foreign_search":
		return c.BEA, "bea", c.BEA != nil
	case "atsb_search":
		return c.ATSB, "atsb", c.ATSB != nil
	default:
		return nil, "", false
	}
}

// RunJob executes one foreign-search job end-to-end and finalizes the crawl_job.
func RunJob(ctx context.Context, db *sql.DB, c Clients, job Job) error {
	client, authority, ok := clientFor(c, job.JobType)
	if !ok {
		recordError(ctx, db, job.ID, "foreignsearch://"+job.JobType, "unknown",
			fmt.Sprintf("no client for job_type %q (country %s)", job.JobType, job.ISO2))
		return finalize(ctx, db, job.ID, "failed", jobStats{})
	}
	recs, err := client.Search(ctx, job.ISO2)
	if err != nil {
		recordError(ctx, db, job.ID, authority+"://"+job.ISO2, "unknown", err.Error())
		return finalize(ctx, db, job.ID, "failed", jobStats{})
	}
	// BEA's notified-events listing is body-wide (not filtered per country — see
	// bea.go's Search doc comment), so its records must stage without the job's
	// country claim (GO-CP-1); NTSB (CAROL Country query param) and ATSB
	// (pre-filtered --source-file export) are genuinely per-country already.
	stageCountryID := job.CountryID
	if authority == "bea" {
		stageCountryID = 0
	}
	staged, err := StageRecords(ctx, db, job.ID, stageCountryID, authority, recs)
	if err != nil {
		_ = finalize(ctx, db, job.ID, "failed", jobStats{})
		return err
	}
	stats := jobStats{Found: len(recs), Staged: staged, Errors: 0}
	status := "success"
	if len(recs) == 0 {
		status = "partial"
		stats.Warning = emitSilentFailTripwire(ctx, db, job.ID, authority)
	}
	return finalize(ctx, db, job.ID, status, stats)
}

// emitSilentFailTripwire prints a grep-able SILENT_FAIL_SUSPECT token to
// stderr for a job that completed (no transport error) but found zero
// records — the shape an authority client broken by e.g. an API/markup
// change takes, which would otherwise be indistinguishable from a genuinely
// empty result at status='success' (GO-CP-4). When the same country+
// authority's previous run found > 0, the message calls out the 100%→0 drop
// distinctly.
func emitSilentFailTripwire(ctx context.Context, db *sql.DB, jobID int64, authority string) string {
	warning := "found_zero"
	msg := fmt.Sprintf("SILENT_FAIL_SUSPECT body=%s job=%d found=0", authority, jobID)
	if prev, ok := previousFound(ctx, db, jobID); ok && prev > 0 {
		warning = "found_zero_regression"
		msg += fmt.Sprintf(" prev_found=%d regression=100pct_to_0", prev)
	}
	fmt.Fprintln(os.Stderr, msg)
	return warning
}

// previousFound looks up the found count from the most recently finished
// prior job for the SAME country and job_type as jobID (this country's
// previous run against the same authority — job_type is already
// authority-specific: ntsb_foreign_search/bea_foreign_search/atsb_search), by
// reading its stats_json. Returns (0, false) when there is no prior finished
// run or its stats can't be read.
func previousFound(ctx context.Context, db *sql.DB, jobID int64) (int, bool) {
	var raw sql.NullString
	err := db.QueryRowContext(ctx, `
		SELECT prev.stats_json
		  FROM crawl_jobs cur
		  JOIN crawl_jobs prev
		    ON prev.country_id = cur.country_id
		   AND prev.job_type = cur.job_type
		   AND prev.id != cur.id
		   AND prev.finished_at IS NOT NULL
		 WHERE cur.id = ?
		 ORDER BY prev.finished_at DESC
		 LIMIT 1`, jobID).Scan(&raw)
	if err != nil || !raw.Valid {
		return 0, false
	}
	var s jobStats
	if json.Unmarshal([]byte(raw.String), &s) != nil {
		return 0, false
	}
	return s.Found, true
}

// ProcessPending runs pending (and stale-running) foreign-search jobs, highest
// country priority first. limit <= 0 means no cap.
func ProcessPending(ctx context.Context, db *sql.DB, c Clients, limit int) (int, error) {
	q := `
		SELECT cj.id, c.id, c.iso2, cj.job_type
		  FROM crawl_jobs cj
		  JOIN countries c ON c.id = cj.country_id
		 WHERE cj.job_type IN ('ntsb_foreign_search','bea_foreign_search','atsb_search')
		   AND (
		         cj.status = 'pending'
		      OR (cj.status = 'running' AND cj.started_at IS NOT NULL
		          AND cj.started_at < (CAST(unixepoch('subsec') * 1000 AS INTEGER) - 3600000))
		       )
		 ORDER BY c.priority_score DESC, c.iso2 ASC`
	if limit > 0 {
		q += fmt.Sprintf(" LIMIT %d", limit)
	}
	rows, err := db.QueryContext(ctx, q)
	if err != nil {
		return 0, fmt.Errorf("foreignsearch: select pending jobs: %w", err)
	}
	var jobs []Job
	for rows.Next() {
		var j Job
		if err := rows.Scan(&j.ID, &j.CountryID, &j.ISO2, &j.JobType); err != nil {
			rows.Close()
			return 0, fmt.Errorf("foreignsearch: scan job: %w", err)
		}
		jobs = append(jobs, j)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return 0, err
	}
	rows.Close()

	processed := 0
	for _, j := range jobs {
		claimed, err := claimJob(ctx, db, j.ID)
		if err != nil {
			return processed, fmt.Errorf("foreignsearch: mark running %d: %w", j.ID, err)
		}
		if !claimed {
			// GO-CP-9: lost the race — another ProcessPending invocation already
			// claimed this job between the SELECT above and this UPDATE. Skip it.
			continue
		}
		if err := RunJob(ctx, db, c, j); err != nil {
			return processed, err
		}
		processed++
	}
	return processed, nil
}

// claimJob atomically transitions job id from claimable (pending, or running
// with a stale started_at — the same two states ProcessPending's SELECT
// treats as eligible) to running. It mirrors that SELECT's WHERE clause in the
// UPDATE itself so the claim is a single atomic compare-and-set: RowsAffected
// tells the caller whether THIS call won the claim, not whether the row was
// merely eligible when read (GO-CP-9 — the prior code claimed unconditionally
// with `WHERE id=?`, letting two overlapping ProcessPending runs both flip the
// same job to running and both execute it).
func claimJob(ctx context.Context, db *sql.DB, jobID int64) (bool, error) {
	res, err := db.ExecContext(ctx, `
		UPDATE crawl_jobs
		   SET status = 'running', started_at = CAST(unixepoch('subsec') * 1000 AS INTEGER)
		 WHERE id = ?
		   AND (
		         status = 'pending'
		      OR (status = 'running' AND started_at IS NOT NULL
		          AND started_at < (CAST(unixepoch('subsec') * 1000 AS INTEGER) - 3600000))
		       )`, jobID)
	if err != nil {
		return false, err
	}
	n, err := res.RowsAffected()
	if err != nil {
		return false, err
	}
	return n > 0, nil
}

func finalize(ctx context.Context, db *sql.DB, jobID int64, status string, stats jobStats) error {
	b, _ := json.Marshal(stats)
	if _, err := db.ExecContext(ctx, `
		UPDATE crawl_jobs SET status = ?, stats_json = ?, finished_at = CAST(unixepoch('subsec')*1000 AS INTEGER)
		 WHERE id = ?`, status, string(b), jobID); err != nil {
		return fmt.Errorf("foreignsearch: finalize job %d: %w", jobID, err)
	}
	return nil
}

func recordError(ctx context.Context, db *sql.DB, jobID int64, url, errType, msg string) {
	_, _ = db.ExecContext(ctx, `
		INSERT INTO crawl_errors (crawl_job_id, url, error_type, message) VALUES (?, ?, ?, ?)`,
		jobID, url, errType, msg)
}

package wayback

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
)

// Job identifies one wayback_cdx crawl job to run.
type Job struct {
	ID        int64
	CountryID int64
	ISO2      string
}

type jobStats struct {
	Found      int `json:"found"`
	Staged     int `json:"staged"`
	Downloaded int `json:"downloaded"`
	Errors     int `json:"errors"`
	// Warning records a GO-CP-4 silent-fail-suspect note when found==0 for a
	// completed (non-error) job. Empty in the normal case.
	Warning string `json:"warning,omitempty"`
}

// RunJob executes one wayback_cdx job end-to-end and finalizes the crawl_job
// row. It returns an error only on an unexpected DB failure; data-level problems
// (no target, transport error, bad document) are recorded against the job.
func RunJob(ctx context.Context, db *sql.DB, f Fetcher, storeDir string, job Job) error {
	target, ok, err := ResolveTarget(ctx, db, job.CountryID)
	if err != nil {
		_ = finalize(ctx, db, job.ID, "failed", jobStats{})
		return err
	}
	if !ok {
		recordError(ctx, db, job.ID, "wayback://"+job.ISO2, "unknown",
			fmt.Sprintf("no wayback target for %s", job.ISO2))
		return finalize(ctx, db, job.ID, "failed", jobStats{})
	}

	raw, err := f.CDX(ctx, target)
	if err != nil {
		recordError(ctx, db, job.ID, target, "unknown", err.Error())
		return finalize(ctx, db, job.ID, "failed", jobStats{})
	}

	snaps, warnings, err := ParseCDX(raw)
	if err != nil {
		recordError(ctx, db, job.ID, target, "parse_error", err.Error())
		return finalize(ctx, db, job.ID, "failed", jobStats{})
	}

	staged, err := StageSnapshots(ctx, db, job.ID, job.CountryID, snaps)
	if err != nil {
		_ = finalize(ctx, db, job.ID, "failed", jobStats{})
		return err
	}

	docs, err := PendingDocs(ctx, db, job.CountryID)
	if err != nil {
		_ = finalize(ctx, db, job.ID, "failed", jobStats{})
		return err
	}
	downloaded, dlErrors := 0, 0
	for _, d := range docs {
		if err := DownloadStaged(ctx, db, f, storeDir, job.ISO2, d); err != nil {
			recordError(ctx, db, job.ID, d.ArchivedURL, "unknown", err.Error())
			dlErrors++
			continue
		}
		downloaded++
	}

	stats := jobStats{Found: len(snaps), Staged: staged, Downloaded: downloaded, Errors: dlErrors}
	status := "success"
	if warnings > 0 || dlErrors > 0 {
		status = "partial"
	}
	if len(snaps) == 0 {
		status = "partial"
		stats.Warning = emitSilentFailTripwire(ctx, db, job.ID, target)
	}
	return finalize(ctx, db, job.ID, status, stats)
}

// emitSilentFailTripwire prints a grep-able SILENT_FAIL_SUSPECT token to
// stderr for a job that completed (no transport/parse error) but found zero
// CDX snapshots — the shape a target broken by e.g. a CDX filter/format
// change takes, which would otherwise be indistinguishable from a genuinely
// empty archive at status='success' (GO-CP-4). When the same country's
// previous wayback_cdx run found > 0, the message calls out the 100%→0 drop
// distinctly.
func emitSilentFailTripwire(ctx context.Context, db *sql.DB, jobID int64, target string) string {
	warning := "found_zero"
	msg := fmt.Sprintf("SILENT_FAIL_SUSPECT body=%s job=%d found=0", target, jobID)
	if prev, ok := previousFound(ctx, db, jobID); ok && prev > 0 {
		warning = "found_zero_regression"
		msg += fmt.Sprintf(" prev_found=%d regression=100pct_to_0", prev)
	}
	fmt.Fprintln(os.Stderr, msg)
	return warning
}

// previousFound looks up the found count from the most recently finished
// prior job for the SAME country and job_type as jobID (this country's
// previous wayback_cdx run), by reading its stats_json. Returns (0, false)
// when there is no prior finished run or its stats can't be read.
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

// ProcessPending runs up to limit pending wayback_cdx jobs, highest country
// priority first. limit <= 0 means no cap.
func ProcessPending(ctx context.Context, db *sql.DB, f Fetcher, storeDir string, limit int) (int, error) {
	q := `
		SELECT cj.id, c.id, c.iso2
		  FROM crawl_jobs cj
		  JOIN countries c ON c.id = cj.country_id
		 WHERE cj.job_type = 'wayback_cdx'
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
		return 0, fmt.Errorf("wayback: select pending jobs: %w", err)
	}
	var jobs []Job
	for rows.Next() {
		var j Job
		if err := rows.Scan(&j.ID, &j.CountryID, &j.ISO2); err != nil {
			rows.Close()
			return 0, fmt.Errorf("wayback: scan job: %w", err)
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
			return processed, fmt.Errorf("wayback: mark running %d: %w", j.ID, err)
		}
		if !claimed {
			// GO-CP-9: lost the race — another ProcessPending invocation (or a
			// prior iteration re-reading a stale row) already claimed this job
			// between the SELECT above and this UPDATE. Skip it, don't touch it.
			continue
		}
		if err := RunJob(ctx, db, f, storeDir, j); err != nil {
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
		UPDATE crawl_jobs
		   SET status = ?, stats_json = ?, finished_at = unixepoch('subsec')*1000
		 WHERE id = ?`, status, string(b), jobID); err != nil {
		return fmt.Errorf("wayback: finalize job %d: %w", jobID, err)
	}
	return nil
}

func recordError(ctx context.Context, db *sql.DB, jobID int64, url, errType, msg string) {
	_, _ = db.ExecContext(ctx, `
		INSERT INTO crawl_errors (crawl_job_id, url, error_type, message)
		VALUES (?, ?, ?, ?)`, jobID, url, errType, msg)
}

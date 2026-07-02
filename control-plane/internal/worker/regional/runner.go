package regional

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
)

// Job identifies one archive_crawl job for a regional_raio country.
type Job struct {
	ID        int64
	CountryID int64
	ISO2      string
	BodyCode  string // ECCAA / BAGAIA / IAC, resolved before RunJob
}

// Clients holds one RegionalClient per supported body. A nil field means the
// body is not wired (jobs routed to it fail with a recorded error).
type Clients struct {
	ECCAA  RegionalClient
	BAGAIA RegionalClient
	IAC    RegionalClient
}

type jobStats struct {
	Found  int `json:"found"`
	Staged int `json:"staged"`
	Errors int `json:"errors"`
	// Warning records a GO-CP-4 silent-fail-suspect note when found==0 for a
	// completed (non-error) job — a parser broken by a site redesign yields 0
	// anchors and would otherwise finalize as an indistinguishable 'success'.
	// Empty in the normal case.
	Warning string `json:"warning,omitempty"`
}

// clientFor returns the client for a body code, or (nil, false) when the code is
// unknown or the client is not wired.
func clientFor(c Clients, bodyCode string) (RegionalClient, bool) {
	var client RegionalClient
	switch bodyCode {
	case "ECCAA":
		client = c.ECCAA
	case "BAGAIA":
		client = c.BAGAIA
	case "IAC":
		client = c.IAC
	default:
		return nil, false
	}
	if client == nil {
		return nil, false
	}
	return client, true
}

// RunJob executes one regional archive_crawl job end-to-end and finalizes the
// crawl_job row. It returns an error only on an unexpected DB failure;
// data-level problems (no client, transport error) are recorded against the job.
func RunJob(ctx context.Context, db *sql.DB, c Clients, job Job) error {
	client, ok := clientFor(c, job.BodyCode)
	if !ok {
		recordError(ctx, db, job.ID, "regional://"+job.BodyCode, "unknown",
			fmt.Sprintf("no regional client for body %q (%s)", job.BodyCode, job.ISO2))
		return finalize(ctx, db, job.ID, "failed", jobStats{})
	}

	recs, warnings, err := client.Search(ctx, job.ISO2)
	if err != nil {
		recordError(ctx, db, job.ID, "regional://"+job.BodyCode, "unknown", err.Error())
		return finalize(ctx, db, job.ID, "failed", jobStats{})
	}

	// Every wired body (ECCAA/BAGAIA/IAC) publishes one body-wide listing, not
	// filtered per country (see the Search doc comments in eccaa.go/bagaia.go/
	// iac.go), so the job's own country must NOT be stamped on the records it
	// stages — see StageRecords' doc comment for why (GO-CP-1).
	staged, err := StageRecords(ctx, db, job.ID, 0, job.BodyCode, recs)
	if err != nil {
		_ = finalize(ctx, db, job.ID, "failed", jobStats{})
		return err
	}

	stats := jobStats{Found: len(recs), Staged: staged, Errors: warnings}
	status := "success"
	if warnings > 0 {
		status = "partial"
	}
	if len(recs) == 0 {
		status = "partial"
		stats.Warning = emitSilentFailTripwire(ctx, db, job.ID, job.BodyCode)
	}
	return finalize(ctx, db, job.ID, status, stats)
}

// emitSilentFailTripwire prints a grep-able SILENT_FAIL_SUSPECT token to
// stderr for a job that completed (no transport/parse error) but found zero
// records — the shape a parser broken by a site redesign takes, which would
// otherwise be indistinguishable from a genuinely empty listing at
// status='success' (GO-CP-4). When the same country's previous archive_crawl
// run found > 0, the message calls out the 100%→0 drop distinctly since that
// is the stronger signal of breakage vs. a body that is just quiet this run.
// Returns the jobStats.Warning value to record alongside it.
func emitSilentFailTripwire(ctx context.Context, db *sql.DB, jobID int64, body string) string {
	warning := "found_zero"
	msg := fmt.Sprintf("SILENT_FAIL_SUSPECT body=%s job=%d found=0", body, jobID)
	if prev, ok := previousFound(ctx, db, jobID); ok && prev > 0 {
		warning = "found_zero_regression"
		msg += fmt.Sprintf(" prev_found=%d regression=100pct_to_0", prev)
	}
	fmt.Fprintln(os.Stderr, msg)
	return warning
}

// previousFound looks up the found count from the most recently finished
// prior job for the SAME country and job_type as jobID (i.e. this country's
// previous archive_crawl run — every wired body is body-wide, so all
// countries routed to the same body see identical counts run-to-run, making
// country_id a simpler and equally valid comparison key than resolving the
// body), by reading its stats_json. Returns (0, false) when there is no prior
// finished run or its stats can't be read — this is a best-effort signal, not
// a correctness dependency.
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

// ProcessPending runs up to limit pending/stale archive_crawl jobs whose country
// is regional_raio, highest country priority first. limit <= 0 means no cap.
// bodyFilter, when non-empty (ECCAA/BAGAIA/IAC), restricts processing to jobs
// whose resolved body matches; others are left pending. This is required with an
// out-of-band --source-file, which is body-specific and would otherwise be
// mis-parsed against the wrong body's origin.
func ProcessPending(ctx context.Context, db *sql.DB, c Clients, limit int, bodyFilter string) (int, error) {
	q := `
		SELECT cj.id, c.id, c.iso2
		  FROM crawl_jobs cj
		  JOIN countries c ON c.id = cj.country_id
		 WHERE cj.job_type = 'archive_crawl'
		   AND c.coverage_status = 'regional_raio'
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
		return 0, fmt.Errorf("regional: select pending jobs: %w", err)
	}
	var jobs []Job
	for rows.Next() {
		var j Job
		if err := rows.Scan(&j.ID, &j.CountryID, &j.ISO2); err != nil {
			rows.Close()
			return 0, fmt.Errorf("regional: scan job: %w", err)
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
		code, ok, err := ResolveBody(ctx, db, j.CountryID)
		if err != nil {
			return processed, err
		}
		if bodyFilter != "" && code != bodyFilter {
			// Not the targeted body (covers the no-body case too); leave pending
			// for a run that targets its body. Do not count as processed.
			continue
		}
		if !ok {
			recordError(ctx, db, j.ID, "regional://"+j.ISO2, "unknown",
				fmt.Sprintf("no regional body for %s", j.ISO2))
			if err := finalize(ctx, db, j.ID, "failed", jobStats{}); err != nil {
				return processed, err
			}
			processed++
			continue
		}
		j.BodyCode = code
		if _, err := db.ExecContext(ctx,
			`UPDATE crawl_jobs SET status='running', started_at=unixepoch('subsec')*1000 WHERE id=?`, j.ID); err != nil {
			return processed, fmt.Errorf("regional: mark running %d: %w", j.ID, err)
		}
		if err := RunJob(ctx, db, c, j); err != nil {
			return processed, err
		}
		processed++
	}
	return processed, nil
}

func finalize(ctx context.Context, db *sql.DB, jobID int64, status string, stats jobStats) error {
	b, _ := json.Marshal(stats)
	if _, err := db.ExecContext(ctx, `
		UPDATE crawl_jobs
		   SET status = ?, stats_json = ?, finished_at = unixepoch('subsec')*1000
		 WHERE id = ?`, status, string(b), jobID); err != nil {
		return fmt.Errorf("regional: finalize job %d: %w", jobID, err)
	}
	return nil
}

func recordError(ctx context.Context, db *sql.DB, jobID int64, url, errType, msg string) {
	_, _ = db.ExecContext(ctx, `
		INSERT INTO crawl_errors (crawl_job_id, url, error_type, message)
		VALUES (?, ?, ?, ?)`, jobID, url, errType, msg)
}

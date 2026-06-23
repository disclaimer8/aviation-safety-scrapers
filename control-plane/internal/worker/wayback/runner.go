package wayback

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
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
}

// RunJob executes one wayback_cdx job end-to-end and finalizes the crawl_job
// row. It returns an error only on an unexpected DB failure; data-level problems
// (no target, transport error, bad document) are recorded against the job.
func RunJob(ctx context.Context, db *sql.DB, f Fetcher, storeDir string, job Job) error {
	target, ok, err := ResolveTarget(ctx, db, job.CountryID)
	if err != nil {
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
		return err
	}

	docs, err := PendingDocs(ctx, db, job.CountryID)
	if err != nil {
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
	return finalize(ctx, db, job.ID, status, stats)
}

// ProcessPending runs up to limit pending wayback_cdx jobs, highest country
// priority first. limit <= 0 means no cap.
func ProcessPending(ctx context.Context, db *sql.DB, f Fetcher, storeDir string, limit int) (int, error) {
	q := `
		SELECT cj.id, c.id, c.iso2
		  FROM crawl_jobs cj
		  JOIN countries c ON c.id = cj.country_id
		 WHERE cj.job_type = 'wayback_cdx' AND cj.status = 'pending'
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
		if _, err := db.ExecContext(ctx,
			`UPDATE crawl_jobs SET status='running', started_at=unixepoch('subsec')*1000 WHERE id=?`, j.ID); err != nil {
			return processed, fmt.Errorf("wayback: mark running %d: %w", j.ID, err)
		}
		if err := RunJob(ctx, db, f, storeDir, j); err != nil {
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
		return fmt.Errorf("wayback: finalize job %d: %w", jobID, err)
	}
	return nil
}

func recordError(ctx context.Context, db *sql.DB, jobID int64, url, errType, msg string) {
	_, _ = db.ExecContext(ctx, `
		INSERT INTO crawl_errors (crawl_job_id, url, error_type, message)
		VALUES (?, ?, ?, ?)`, jobID, url, errType, msg)
}

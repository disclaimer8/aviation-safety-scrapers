package foreignsearch

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
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
	return finalize(ctx, db, job.ID, "success", stats)
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
		if _, err := db.ExecContext(ctx,
			`UPDATE crawl_jobs SET status='running', started_at=CAST(unixepoch('subsec')*1000 AS INTEGER) WHERE id=?`, j.ID); err != nil {
			return processed, fmt.Errorf("foreignsearch: mark running %d: %w", j.ID, err)
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

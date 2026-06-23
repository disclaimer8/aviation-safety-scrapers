package regional

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
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

	recs, err := client.Search(ctx, job.ISO2)
	if err != nil {
		recordError(ctx, db, job.ID, "regional://"+job.BodyCode, "unknown", err.Error())
		return finalize(ctx, db, job.ID, "failed", jobStats{})
	}

	staged, err := StageRecords(ctx, db, job.ID, job.CountryID, job.BodyCode, recs)
	if err != nil {
		_ = finalize(ctx, db, job.ID, "failed", jobStats{})
		return err
	}

	return finalize(ctx, db, job.ID, "success", jobStats{Found: len(recs), Staged: staged})
}

// ProcessPending runs up to limit pending/stale archive_crawl jobs whose country
// is regional_raio, highest country priority first. limit <= 0 means no cap.
func ProcessPending(ctx context.Context, db *sql.DB, c Clients, limit int) (int, error) {
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

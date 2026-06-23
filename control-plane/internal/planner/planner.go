package planner

import (
	"context"
	"database/sql"
	"fmt"
	"time"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/model"
)

// Candidate is a non-excluded country eligible for scheduling.
type Candidate struct {
	CountryID      int64
	ISO2           string
	CoverageStatus model.CoverageStatus
	PriorityScore  float64
	DelegateISO2   string
	RefreshCadence string
}

// Candidates returns all schedulable countries (policy_status != 'excluded')
// ordered by priority_score DESC, iso2 ASC.
func Candidates(ctx context.Context, db *sql.DB) ([]Candidate, error) {
	rows, err := db.QueryContext(ctx, `
		SELECT id, iso2, coverage_status, priority_score,
		       COALESCE(delegate_iso2, ''), COALESCE(refresh_cadence, '')
		  FROM countries
		 WHERE policy_status != 'excluded'
		 ORDER BY priority_score DESC, iso2 ASC
	`)
	if err != nil {
		return nil, fmt.Errorf("planner: query candidates: %w", err)
	}
	defer rows.Close()

	var out []Candidate
	for rows.Next() {
		var c Candidate
		var cov string
		if err := rows.Scan(&c.CountryID, &c.ISO2, &cov, &c.PriorityScore,
			&c.DelegateISO2, &c.RefreshCadence); err != nil {
			return nil, fmt.Errorf("planner: scan candidate: %w", err)
		}
		c.CoverageStatus = model.CoverageStatus(cov)
		out = append(out, c)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("planner: iterate candidates: %w", err)
	}
	return out, nil
}

// sourceNameByJobType maps each job type to the name of the sources row that
// represents its acquisition channel. Names must match the seeded rows.
var sourceNameByJobType = map[model.CrawlJobType]string{
	model.CrawlJobTypeAuthorityHealthCheck: "Authority Health Check (method)",
	model.CrawlJobTypeArchiveCrawl:         "Authority Archive Crawl (method)",
	model.CrawlJobTypeWaybackCDX:           "Wayback Machine CDX (method)",
	model.CrawlJobTypePDFDiscovery:         "Scholarly PDF Discovery (method)",
	model.CrawlJobTypeICAOELibrarySearch:   "ICAO e-Library Final Reports",
	model.CrawlJobTypeDirectRequestNeeded:  "Direct Authority Request (method)",
	model.CrawlJobTypeNTSBForeignSearch:    "NTSB Foreign Investigations (method)",
	model.CrawlJobTypeBEAForeignSearch:     "BEA Foreign Investigations (method)",
	model.CrawlJobTypeATSBSearch:           "ATSB Foreign Investigations (method)",
}

// SourceResolver maps a job type to a sources.id, loaded once from the DB.
type SourceResolver struct {
	byJobType map[model.CrawlJobType]int64
}

// NewSourceResolver loads the job-type → source-id map. A job type whose source
// row is missing is simply absent from the map (Resolve returns false).
func NewSourceResolver(ctx context.Context, db *sql.DB) (*SourceResolver, error) {
	r := &SourceResolver{byJobType: make(map[model.CrawlJobType]int64, len(sourceNameByJobType))}
	for jt, name := range sourceNameByJobType {
		var id int64
		err := db.QueryRowContext(ctx, `SELECT id FROM sources WHERE name = ?`, name).Scan(&id)
		if err == sql.ErrNoRows {
			continue
		}
		if err != nil {
			return nil, fmt.Errorf("planner: resolve source %q: %w", name, err)
		}
		r.byJobType[jt] = id
	}
	return r, nil
}

// Resolve returns the source id for a job type, or false if none is mapped.
func (r *SourceResolver) Resolve(jobType model.CrawlJobType) (int64, bool) {
	id, ok := r.byJobType[jobType]
	return id, ok
}

const dayMs = int64(24 * 60 * 60 * 1000)

func cadenceDurationMs(cadence string) int64 {
	switch cadence {
	case "weekly":
		return 7 * dayMs
	case "biweekly":
		return 14 * dayMs
	case "monthly":
		return 30 * dayMs
	case "quarterly":
		return 90 * dayMs
	default:
		return 90 * dayMs
	}
}

func cadenceElapsed(nowMs, lastFinishedMs int64, cadence string) bool {
	return nowMs-lastFinishedMs >= cadenceDurationMs(cadence)
}

// JobState summarises the existing crawl_jobs for a (country, job_type) pair.
type JobState struct {
	HasActive        bool  // a pending or running job exists
	HasTerminal      bool  // at least one success/failed/partial job exists
	LastFinishedAtMs int64 // max(finished_at) among terminal jobs (0 if none)
}

// JobStateFor inspects crawl_jobs for the given country and job type.
func JobStateFor(ctx context.Context, db *sql.DB, countryID int64, jobType model.CrawlJobType) (JobState, error) {
	var st JobState
	var active int
	if err := db.QueryRowContext(ctx, `
		SELECT COUNT(*) FROM crawl_jobs
		 WHERE country_id = ? AND job_type = ? AND status IN ('pending','running')
	`, countryID, string(jobType)).Scan(&active); err != nil {
		return st, fmt.Errorf("planner: job active count: %w", err)
	}
	st.HasActive = active > 0

	var cnt int
	var maxFinished sql.NullInt64
	if err := db.QueryRowContext(ctx, `
		SELECT COUNT(*), MAX(finished_at) FROM crawl_jobs
		 WHERE country_id = ? AND job_type = ?
		   AND status IN ('success','failed','partial')
	`, countryID, string(jobType)).Scan(&cnt, &maxFinished); err != nil {
		return st, fmt.Errorf("planner: job terminal state: %w", err)
	}
	st.HasTerminal = cnt > 0
	if maxFinished.Valid {
		st.LastFinishedAtMs = maxFinished.Int64
	}
	return st, nil
}

// Decision records why a candidate job is or is not enqueued.
type Decision string

const (
	DecisionWouldEnqueue    Decision = "would_enqueue"
	DecisionSkippedActive   Decision = "skipped_active"
	DecisionSkippedCadence  Decision = "skipped_cadence"
	DecisionSkippedNoSource Decision = "skipped_no_source"
)

// PlannedJob is one (country, job_type) decision.
type PlannedJob struct {
	CountryID      int64                `json:"-"`
	ISO2           string               `json:"iso2"`
	PriorityScore  float64              `json:"priority_score"`
	CoverageStatus model.CoverageStatus `json:"coverage_status"`
	JobType        model.CrawlJobType   `json:"job_type"`
	SourceID       int64                `json:"source_id"`
	Decision       Decision             `json:"decision"`
}

// Plan is the full scheduling plan.
type Plan struct {
	GeneratedAt        time.Time    `json:"generated_at"`
	CandidateCountries int          `json:"candidate_countries"`
	JobsPlanned        int          `json:"jobs_planned"`
	Jobs               []PlannedJob `json:"jobs"`
	Warnings           []string     `json:"warnings"`
}

// BuildPlan ranks gaps and produces a decision per (country, job_type). It does
// not write anything; pass the result to Enqueue to persist would_enqueue rows.
// limit <= 0 means no country cap.
func BuildPlan(ctx context.Context, db *sql.DB, nowMs int64, limit int) (Plan, error) {
	cands, err := Candidates(ctx, db)
	if err != nil {
		return Plan{}, err
	}
	if limit > 0 && len(cands) > limit {
		cands = cands[:limit]
	}
	resolver, err := NewSourceResolver(ctx, db)
	if err != nil {
		return Plan{}, err
	}

	plan := Plan{
		GeneratedAt:        time.UnixMilli(nowMs).UTC(),
		CandidateCountries: len(cands),
		Warnings:           []string{},
		Jobs:               []PlannedJob{},
	}

	for _, c := range cands {
		for _, jt := range JobTypesFor(c.CoverageStatus, c.DelegateISO2) {
			pj := PlannedJob{
				CountryID:      c.CountryID,
				ISO2:           c.ISO2,
				PriorityScore:  c.PriorityScore,
				CoverageStatus: c.CoverageStatus,
				JobType:        jt,
			}
			sourceID, ok := resolver.Resolve(jt)
			if !ok {
				pj.Decision = DecisionSkippedNoSource
				plan.Warnings = append(plan.Warnings,
					fmt.Sprintf("%s/%s: no source resolved", c.ISO2, jt))
				plan.Jobs = append(plan.Jobs, pj)
				continue
			}
			pj.SourceID = sourceID

			state, err := JobStateFor(ctx, db, c.CountryID, jt)
			if err != nil {
				return Plan{}, err
			}
			switch {
			case state.HasActive:
				pj.Decision = DecisionSkippedActive
			case state.HasTerminal && !cadenceElapsed(nowMs, state.LastFinishedAtMs, c.RefreshCadence):
				pj.Decision = DecisionSkippedCadence
			default:
				pj.Decision = DecisionWouldEnqueue
				plan.JobsPlanned++
			}
			plan.Jobs = append(plan.Jobs, pj)
		}
	}
	return plan, nil
}

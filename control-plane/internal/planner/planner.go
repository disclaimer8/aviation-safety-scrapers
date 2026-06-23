package planner

import (
	"context"
	"database/sql"
	"fmt"

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

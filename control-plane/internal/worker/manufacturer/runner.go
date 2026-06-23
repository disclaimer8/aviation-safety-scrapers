package manufacturer

import (
	"context"
	"database/sql"
	"strconv"
)

// Result summarises the outcome of a single ProcessManufacturer run.
type Result struct {
	Found  int // total records (discovered + probed)
	Staged int // newly inserted rows
	Errors int // non-fatal errors (e.g. probe failure)
}

// Discoverer is the interface consumed by ProcessManufacturer. *Client satisfies
// it structurally.
type Discoverer interface {
	// Discover returns all currently published records from the manufacturer's
	// listing page.
	Discover(ctx context.Context) ([]ManufacturerRecord, error)

	// ProbeNextIssue checks whether issue (highestKnown+1) exists. It returns
	// (record, true, nil) when found, (zero, false, nil) when not found, and
	// (zero, false, err) on transport or scheme error.
	ProbeNextIssue(ctx context.Context, highestKnown int) (ManufacturerRecord, bool, error)
}

// ProcessManufacturer fetches all Airbus Safety First issues, probes for the
// next unreleased issue, stages everything into the database, and returns a
// summary Result.
//
// A Discover error is fatal and returned immediately. A ProbeNextIssue error is
// non-fatal: it is counted in Result.Errors and the already-discovered records
// are still staged.
func ProcessManufacturer(ctx context.Context, db *sql.DB, d Discoverer) (Result, error) {
	recs, err := d.Discover(ctx)
	if err != nil {
		return Result{}, err
	}

	highest := highestNumericIssue(recs)

	var errs int
	probeRec, found, probeErr := d.ProbeNextIssue(ctx, highest)
	if probeErr != nil {
		errs++
	} else if found {
		recs = append(recs, probeRec)
	}

	staged, stageErr := StageRecords(ctx, db, "airbus", "safety_first", recs)
	if stageErr != nil {
		return Result{}, stageErr
	}

	return Result{Found: len(recs), Staged: staged, Errors: errs}, nil
}

// highestNumericIssue returns the largest integer IssueRef found in recs.
// Non-numeric IssueRef values (special editions) are skipped. Returns 0 when
// no numeric issue is present.
func highestNumericIssue(recs []ManufacturerRecord) int {
	highest := 0
	for _, r := range recs {
		n, err := strconv.Atoi(r.IssueRef)
		if err != nil {
			continue
		}
		if n > highest {
			highest = n
		}
	}
	return highest
}

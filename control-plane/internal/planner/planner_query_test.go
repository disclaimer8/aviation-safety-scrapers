package planner

import (
	"context"
	"database/sql"
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/database"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/migrations"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/seed"
)

func seededDB(t *testing.T) (context.Context, *sql.DB) {
	t.Helper()
	ctx := context.Background()
	db, err := database.Open(t.TempDir() + "/coverage.db")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if err := migrations.Apply(ctx, db); err != nil {
		t.Fatal(err)
	}
	if _, err := seed.Apply(ctx, db); err != nil {
		t.Fatal(err)
	}
	return ctx, db
}

func TestCandidatesRankedAndFiltered(t *testing.T) {
	ctx, db := seededDB(t)

	cands, err := Candidates(ctx, db)
	if err != nil {
		t.Fatal(err)
	}
	if len(cands) == 0 {
		t.Fatal("no candidates")
	}

	// Excluded countries (AF, KP, SY) must never appear.
	for _, c := range cands {
		if c.ISO2 == "AF" || c.ISO2 == "KP" || c.ISO2 == "SY" {
			t.Fatalf("excluded country %s present in candidates", c.ISO2)
		}
	}

	// Ordering: priority_score descending, iso2 ascending tiebreak.
	for i := 1; i < len(cands); i++ {
		prev, cur := cands[i-1], cands[i]
		if cur.PriorityScore > prev.PriorityScore {
			t.Fatalf("not sorted by priority desc at %d: %v > %v", i, cur.PriorityScore, prev.PriorityScore)
		}
		if cur.PriorityScore == prev.PriorityScore && cur.ISO2 < prev.ISO2 {
			t.Fatalf("tiebreak not iso2 asc at %d: %s < %s", i, cur.ISO2, prev.ISO2)
		}
	}
}

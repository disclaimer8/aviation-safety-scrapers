package extract

import (
	"context"
	"testing"
)

// TestPendingDocsScansFractionalPriority guards the regression where
// ExtractDoc.Priority was an int64 while countries.priority_score is REAL: real
// (non-integer) scores like 7.5 made the PendingDocs row scan fail with
// "converting driver.Value type float64 to int64". Seed data had only
// integer-valued scores (default 0), so the unit suite missed it; this seeds a
// fractional score and asserts the scan succeeds and the value is preserved.
func TestPendingDocsScansFractionalPriority(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	_, countryID := seedDownloadedDoc(t, db, "ZP", "fracprio")
	if _, err := db.ExecContext(ctx,
		`UPDATE countries SET priority_score = 7.5 WHERE id = ?`, countryID); err != nil {
		t.Fatal(err)
	}

	docs, err := WaybackSource{}.PendingDocs(ctx, db, 0)
	if err != nil {
		t.Fatalf("PendingDocs scan failed on fractional priority_score: %v", err)
	}
	if len(docs) != 1 {
		t.Fatalf("expected 1 doc, got %d", len(docs))
	}
	if docs[0].Priority != 7.5 {
		t.Errorf("Priority = %v, want 7.5", docs[0].Priority)
	}
}

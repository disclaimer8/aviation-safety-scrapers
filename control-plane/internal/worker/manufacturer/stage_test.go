package manufacturer

import (
	"context"
	"database/sql"
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/database"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/migrations"
)

func seededManufacturerDB(t *testing.T) (context.Context, *sql.DB) {
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
	return ctx, db
}

func TestStageRecordsDedups(t *testing.T) {
	ctx, db := seededManufacturerDB(t)
	recs := []ManufacturerRecord{
		{
			IssueRef:        "41",
			Title:           "Safety First #41",
			PublicationDate: "2024-01-15",
			OriginalURL:     "https://airbus.com/safety/41",
			ReportURL:       "https://airbus.com/safety/41.pdf",
		},
		{
			IssueRef:        "42",
			Title:           "Safety First #42",
			PublicationDate: "2024-02-15",
			OriginalURL:     "https://airbus.com/safety/42",
			ReportURL:       "https://airbus.com/safety/42.pdf",
		},
	}

	// Stage 2 records, should insert 2
	n, err := StageRecords(ctx, db, "Airbus", "Safety First", recs)
	if err != nil {
		t.Fatal(err)
	}
	if n != 2 {
		t.Fatalf("staged = %d, want 2", n)
	}

	// Stage same records again, should insert 0 (ON CONFLICT DO NOTHING)
	n2, err := StageRecords(ctx, db, "Airbus", "Safety First", recs)
	if err != nil {
		t.Fatal(err)
	}
	if n2 != 0 {
		t.Fatalf("re-stage = %d, want 0", n2)
	}

	// Verify total count
	var total int
	if err := db.QueryRowContext(ctx, `SELECT COUNT(*) FROM staged_manufacturer_documents`).Scan(&total); err != nil {
		t.Fatal(err)
	}
	if total != 2 {
		t.Fatalf("total = %d, want 2", total)
	}

	// Verify the rows are distinct
	var count41, count42 int
	if err := db.QueryRowContext(ctx, `SELECT COUNT(*) FROM staged_manufacturer_documents WHERE issue_ref=?`, "41").Scan(&count41); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRowContext(ctx, `SELECT COUNT(*) FROM staged_manufacturer_documents WHERE issue_ref=?`, "42").Scan(&count42); err != nil {
		t.Fatal(err)
	}
	if count41 != 1 || count42 != 1 {
		t.Fatalf("issue_ref counts: 41=%d, 42=%d; want 1,1", count41, count42)
	}
}

func TestStageRecordsNullableFields(t *testing.T) {
	ctx, db := seededManufacturerDB(t)
	recs := []ManufacturerRecord{
		{
			IssueRef:    "special",
			Title:       "Special Edition",
			OriginalURL: "https://airbus.com/special",
			// PublicationDate and ReportURL are empty strings; should be NULL
		},
	}

	n, err := StageRecords(ctx, db, "Airbus", "Safety First", recs)
	if err != nil {
		t.Fatal(err)
	}
	if n != 1 {
		t.Fatalf("staged = %d, want 1", n)
	}

	var pubDate, reportURL sql.NullString
	if err := db.QueryRowContext(ctx, `SELECT publication_date, report_url FROM staged_manufacturer_documents WHERE issue_ref=?`, "special").Scan(&pubDate, &reportURL); err != nil {
		t.Fatal(err)
	}
	if pubDate.Valid || reportURL.Valid {
		t.Fatalf("expected NULL values, got publication_date.Valid=%v, report_url.Valid=%v", pubDate.Valid, reportURL.Valid)
	}
}

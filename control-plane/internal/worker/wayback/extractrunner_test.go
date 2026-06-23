package wayback

import (
	"context"
	"database/sql"
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func goodEvent() ExtractedEvent {
	return ExtractedEvent{
		IsAviationAccident: true, Date: "2019-03-10", DatePrecision: "exact",
		Location: "Bishoftu", AircraftRegistration: "ET-AVJ", AircraftType: "B738",
		Fatalities: intp(157), EventType: "accident", ReportType: "final", Language: "en",
	}
}

func TestExtractOneHappyPath(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID, _ := seedDownloadedDoc(t, db, "KE", "k1")
	// Put a real PDF file where local_file_path points (the runner reads it).
	writePDF(t, db, docID)
	doc := loadDoc(t, db, docID)

	status, err := ExtractOne(ctx, db, &fixtureOCRClient{Text: "REPORT"}, &fixtureLLMClient{Event: goodEvent()}, t.TempDir(), doc)
	if err != nil {
		t.Fatalf("ExtractOne: %v", err)
	}
	if status != "extracted" {
		t.Fatalf("status=%q want extracted", status)
	}
	var n int
	db.QueryRowContext(ctx, `SELECT count(*) FROM events`).Scan(&n)
	if n != 1 {
		t.Fatalf("events=%d want 1", n)
	}
}

func TestExtractOneSkipsNonAccident(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID, _ := seedDownloadedDoc(t, db, "KE", "k1")
	writePDF(t, db, docID)
	doc := loadDoc(t, db, docID)

	status, err := ExtractOne(ctx, db, &fixtureOCRClient{Text: "INDEX"}, &fixtureLLMClient{Event: ExtractedEvent{IsAviationAccident: false}}, t.TempDir(), doc)
	if err != nil {
		t.Fatalf("ExtractOne: %v", err)
	}
	if status != "skipped" {
		t.Fatalf("status=%q want skipped", status)
	}
}

func TestExtractOneOCRFailureCountsAttempt(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID, _ := seedDownloadedDoc(t, db, "KE", "k1")
	writePDF(t, db, docID)
	doc := loadDoc(t, db, docID)

	status, err := ExtractOne(ctx, db, &fixtureOCRClient{Err: errors.New("boom")}, &fixtureLLMClient{}, t.TempDir(), doc)
	if err != nil {
		t.Fatalf("ExtractOne returned err: %v", err) // data failures are recorded, not returned
	}
	if status != "failed" {
		t.Fatalf("status=%q want failed", status)
	}
	var attempts int
	var estatus string
	db.QueryRowContext(ctx, `SELECT extraction_attempts, extraction_status FROM staged_wayback_documents WHERE id=?`, docID).Scan(&attempts, &estatus)
	if attempts != 1 || estatus != "failed" {
		t.Fatalf("attempts=%d status=%q", attempts, estatus)
	}
}

func TestProcessExtractPendingResumesFromOCRText(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID, _ := seedDownloadedDoc(t, db, "KE", "k1")
	// Simulate a crashed-after-OCR doc: text already persisted, status ocr_done.
	store := t.TempDir()
	if _, err := PersistOCRText(ctx, db, store, "KE", "k1", docID, "REPORT"); err != nil {
		t.Fatal(err)
	}
	// OCR client that would error if called — proves OCR is skipped on resume.
	stats, err := ProcessExtractPending(ctx, db, &fixtureOCRClient{Err: errors.New("should not be called")},
		&fixtureLLMClient{Event: goodEvent()}, store, 0)
	if err != nil {
		t.Fatalf("ProcessExtractPending: %v", err)
	}
	if stats.Extracted != 1 {
		t.Fatalf("stats=%+v want Extracted 1", stats)
	}
}

// writePDF creates a file at the doc's local_file_path so the runner can read it.
func writePDF(t *testing.T, db *sql.DB, docID int64) {
	t.Helper()
	var p string
	if err := db.QueryRowContext(context.Background(),
		`SELECT local_file_path FROM staged_wayback_documents WHERE id=?`, docID).Scan(&p); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepathDir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte("%PDF-1.4 fake"), 0o644); err != nil {
		t.Fatal(err)
	}
}

func filepathDir(p string) string { return filepath.Dir(p) }

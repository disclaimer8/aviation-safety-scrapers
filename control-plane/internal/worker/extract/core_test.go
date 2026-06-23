package extract

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

	status, err := extractOne(ctx, db, WaybackSource{}, &fixtureOCRClient{Text: "REPORT"}, &fixtureLLMClient{Event: goodEvent()}, t.TempDir(), doc)
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

	status, err := extractOne(ctx, db, WaybackSource{}, &fixtureOCRClient{Text: "INDEX"}, &fixtureLLMClient{Event: ExtractedEvent{IsAviationAccident: false}}, t.TempDir(), doc)
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

	status, err := extractOne(ctx, db, WaybackSource{}, &fixtureOCRClient{Err: errors.New("boom")}, &fixtureLLMClient{}, t.TempDir(), doc)
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
	if _, err := PersistOCRText(ctx, db, WaybackSource{}, store, "KE", "k1", docID, "REPORT"); err != nil {
		t.Fatal(err)
	}
	// OCR client that would error if called — proves OCR is skipped on resume.
	stats, err := ProcessExtractPending(ctx, db, &fixtureOCRClient{Err: errors.New("should not be called")},
		&fixtureLLMClient{Event: goodEvent()}, store, 0, WaybackSource{})
	if err != nil {
		t.Fatalf("ProcessExtractPending: %v", err)
	}
	if stats.Extracted != 1 {
		t.Fatalf("stats=%+v want Extracted 1", stats)
	}
}

// TestExtractOneOCRFailureWritesCrawlError drives one extraction failure through
// the wayback adapter and asserts a crawl_errors row IS written with an
// error_type that satisfies the CHECK constraint. This is the regression guard
// for the bug where the four extract classifications ("transport"/"ocr"/"llm"/
// "parse") violated the CHECK and were silently swallowed (no row written).
func TestExtractOneOCRFailureWritesCrawlError(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID, _ := seedDownloadedDoc(t, db, "KE", "k1")
	writePDF(t, db, docID)
	doc := loadDoc(t, db, docID)

	// Injected OCR client that errors — forces the failure path.
	status, err := extractOne(ctx, db, WaybackSource{}, &fixtureOCRClient{Err: errors.New("ocr boom")}, &fixtureLLMClient{}, t.TempDir(), doc)
	if err != nil {
		t.Fatalf("extractOne returned err: %v", err)
	}
	if status != "failed" {
		t.Fatalf("status=%q want failed", status)
	}

	var n int
	var errType, message string
	if err := db.QueryRowContext(ctx, `SELECT count(*) FROM crawl_errors`).Scan(&n); err != nil {
		t.Fatal(err)
	}
	if n != 1 {
		t.Fatalf("crawl_errors rows=%d want 1 (row was silently dropped by CHECK violation)", n)
	}
	if err := db.QueryRowContext(ctx, `SELECT error_type, message FROM crawl_errors LIMIT 1`).Scan(&errType, &message); err != nil {
		t.Fatal(err)
	}
	// The CHECK accepts these; the OCR/LLM/transport cause has no granular member
	// so it maps to 'unknown', with the detail carried in message.
	valid := map[string]bool{
		"tls_error": true, "timeout": true, "dns_error": true, "nx_domain": true,
		"http_403": true, "http_404": true, "http_500": true, "parse_error": true,
		"robots_blocked": true, "unknown": true,
	}
	if !valid[errType] {
		t.Fatalf("error_type=%q violates crawl_errors CHECK constraint", errType)
	}
	if message == "" {
		t.Fatalf("message empty: detailed cause must be preserved in message text")
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

package extract

import (
	"context"
	"database/sql"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"
)

// seedManufacturerDoc inserts one staged_manufacturer_documents row. Unlike the
// country-driven sources there is NO country, regional_body, source, or
// crawl_job to seed — manufacturer docs are global.
func seedManufacturerDoc(t *testing.T, db *sql.DB, manufacturer, publication, issueRef, reportURL string) int64 {
	t.Helper()
	ctx := context.Background()
	res, err := db.ExecContext(ctx, `
		INSERT INTO staged_manufacturer_documents
			(manufacturer, publication, issue_ref, title, original_url, report_url)
		VALUES (?, ?, ?, ?, ?, ?)`,
		manufacturer, publication, issueRef,
		fmt.Sprintf("%s #%s", publication, issueRef),
		fmt.Sprintf("https://safetyfirst.example/%s", issueRef),
		reportURL)
	if err != nil {
		t.Fatal(err)
	}
	id, _ := res.LastInsertId()
	return id
}

// ─── slug ────────────────────────────────────────────────────────────────────

func TestManufacturerSlug(t *testing.T) {
	cases := map[string]string{
		"Airbus":       "airbus",
		"Safety First": "safety-first",
		"  ATR  ":      "atr",
		"A&B / C":      "a-b-c",
		"":             "",
	}
	for in, want := range cases {
		if got := manufacturerSlug(in); got != want {
			t.Errorf("manufacturerSlug(%q)=%q want %q", in, got, want)
		}
	}
}

// ─── PendingDocs ─────────────────────────────────────────────────────────────

func TestManufacturerPendingDocsMapsFields(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	reportURL := "https://s3.example/safety_first_41.pdf"
	docID := seedManufacturerDoc(t, db, "Airbus", "Safety First", "41", reportURL)

	src := ManufacturerSource{HTTP: http.DefaultClient}
	docs, err := src.PendingDocs(ctx, db, 0)
	if err != nil {
		t.Fatalf("PendingDocs: %v", err)
	}
	if len(docs) != 1 {
		t.Fatalf("expected 1 doc, got %d", len(docs))
	}
	d := docs[0]
	if d.ID != docID {
		t.Fatalf("doc id=%d want %d", d.ID, docID)
	}
	if d.ArchivedURL != reportURL {
		t.Fatalf("ArchivedURL=%q want %q (report_url is the download target)", d.ArchivedURL, reportURL)
	}
	if d.SourceRef != "Airbus" {
		t.Fatalf("SourceRef=%q want Airbus", d.SourceRef)
	}
	if d.ISO2 != "airbus" {
		t.Fatalf("ISO2=%q want airbus (manufacturer slug, store-dir segment)", d.ISO2)
	}
	// Global doc: no country/job.
	if d.CountryID != 0 {
		t.Fatalf("CountryID=%d want 0 (global doc)", d.CountryID)
	}
	if d.Priority != manufacturerPriority {
		t.Fatalf("Priority=%v want %v", d.Priority, manufacturerPriority)
	}
}

func TestManufacturerPendingDocsExcludesNullReportURL(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	// report_url NULL → not extractable (PDF-only adapter).
	_, err := db.ExecContext(ctx, `
		INSERT INTO staged_manufacturer_documents
			(manufacturer, publication, issue_ref, title, original_url)
		VALUES ('Airbus', 'Safety First', '99', 'no pdf', 'https://safetyfirst.example/99')`)
	if err != nil {
		t.Fatal(err)
	}
	src := ManufacturerSource{HTTP: http.DefaultClient}
	docs, err := src.PendingDocs(ctx, db, 0)
	if err != nil {
		t.Fatalf("PendingDocs: %v", err)
	}
	if len(docs) != 0 {
		t.Fatalf("expected 0 docs (report_url NULL), got %d", len(docs))
	}
}

func TestManufacturerPendingDocsExcludesMaxAttempts(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID := seedManufacturerDoc(t, db, "Airbus", "Safety First", "41", "https://s3.example/41.pdf")
	db.ExecContext(ctx, `UPDATE staged_manufacturer_documents SET extraction_attempts=3 WHERE id=?`, docID)

	src := ManufacturerSource{HTTP: http.DefaultClient}
	docs, err := src.PendingDocs(ctx, db, 0)
	if err != nil {
		t.Fatalf("PendingDocs: %v", err)
	}
	if len(docs) != 0 {
		t.Fatalf("expected 0 docs (max attempts), got %d", len(docs))
	}
}

func TestManufacturerPendingDocsTwoPhaseFlow(t *testing.T) {
	allowLoopback(t)
	ctx := context.Background()
	db := newExtractTestDB(t)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/pdf")
		fmt.Fprint(w, "%PDF-1.4 fake safety first issue")
	}))
	defer srv.Close()

	docID := seedManufacturerDoc(t, db, "Airbus", "Safety First", "41", srv.URL+"/41.pdf")
	src := ManufacturerSource{HTTP: srv.Client()}

	// Phase 1: pending row returned.
	docs, err := src.PendingDocs(ctx, db, 0)
	if err != nil || len(docs) != 1 {
		t.Fatalf("phase1 PendingDocs: docs=%d err=%v", len(docs), err)
	}
	doc := docs[0]

	// Download → download_status='downloaded'.
	if err := src.EnsureDownloaded(ctx, db, t.TempDir(), &doc); err != nil {
		t.Fatalf("EnsureDownloaded: %v", err)
	}

	// Phase 2: same row still returned (extraction phase).
	docs2, _ := src.PendingDocs(ctx, db, 0)
	if len(docs2) != 1 || docs2[0].ID != docID {
		t.Fatalf("phase2: downloaded doc must still be returned, got %d docs", len(docs2))
	}

	// Phase 3: extracted row not returned.
	db.ExecContext(ctx, `UPDATE staged_manufacturer_documents SET extraction_status='extracted' WHERE id=?`, docID)
	docs3, _ := src.PendingDocs(ctx, db, 0)
	if len(docs3) != 0 {
		t.Fatalf("phase3: extracted doc must NOT be returned, got %d docs", len(docs3))
	}
}

// ─── EnsureDownloaded ────────────────────────────────────────────────────────

func TestManufacturerEnsureDownloadedFetchesAndUpdatesRow(t *testing.T) {
	allowLoopback(t)
	ctx := context.Background()
	db := newExtractTestDB(t)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/pdf")
		fmt.Fprint(w, "%PDF-1.4 fake manufacturer report")
	}))
	defer srv.Close()

	docID := seedManufacturerDoc(t, db, "Airbus", "Safety First", "41", srv.URL+"/41.pdf")
	src := ManufacturerSource{HTTP: srv.Client()}

	docs, _ := src.PendingDocs(ctx, db, 0)
	doc := docs[0]
	storeDir := t.TempDir()
	if err := src.EnsureDownloaded(ctx, db, storeDir, &doc); err != nil {
		t.Fatalf("EnsureDownloaded: %v", err)
	}
	if doc.LocalFilePath == "" || doc.Digest == "" {
		t.Fatalf("LocalFilePath=%q Digest=%q both must be set", doc.LocalFilePath, doc.Digest)
	}
	if !filepath.IsAbs(doc.LocalFilePath) {
		t.Fatalf("LocalFilePath must be absolute, got %q", doc.LocalFilePath)
	}
	// File must land under the manufacturer-slug sub-directory.
	if filepath.Base(filepath.Dir(doc.LocalFilePath)) != "airbus" {
		t.Fatalf("file must be under airbus/ store sub-dir, got %q", doc.LocalFilePath)
	}
	var dbStatus, dbPath, dbDigest string
	db.QueryRowContext(ctx,
		`SELECT download_status, local_file_path, digest FROM staged_manufacturer_documents WHERE id=?`, docID).
		Scan(&dbStatus, &dbPath, &dbDigest)
	if dbStatus != "downloaded" || dbPath == "" || dbDigest == "" {
		t.Fatalf("row status=%q path=%q digest=%q", dbStatus, dbPath, dbDigest)
	}
}

func TestManufacturerEnsureDownloadedFailureMarksRow(t *testing.T) {
	allowLoopback(t)
	ctx := context.Background()
	db := newExtractTestDB(t)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
	}))
	defer srv.Close()

	docID := seedManufacturerDoc(t, db, "Airbus", "Safety First", "41", srv.URL+"/missing.pdf")
	src := ManufacturerSource{HTTP: srv.Client()}
	docs, _ := src.PendingDocs(ctx, db, 0)
	doc := docs[0]
	if err := src.EnsureDownloaded(ctx, db, t.TempDir(), &doc); err == nil {
		t.Fatal("expected error from 404")
	}
	var dbStatus string
	db.QueryRowContext(ctx, `SELECT download_status FROM staged_manufacturer_documents WHERE id=?`, docID).Scan(&dbStatus)
	if dbStatus != "failed" {
		t.Fatalf("download_status=%q want failed", dbStatus)
	}
}

// ─── ResolveSource ───────────────────────────────────────────────────────────

func TestManufacturerResolveSourceCreatesManufacturerSource(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID := seedManufacturerDoc(t, db, "Airbus", "Safety First", "41", "https://s3.example/41.pdf")

	src := ManufacturerSource{HTTP: http.DefaultClient}
	doc := ExtractDoc{ID: docID, SourceRef: "Airbus", ISO2: "airbus"}
	id, tier, copyright, err := src.ResolveSource(ctx, db, doc)
	if err != nil {
		t.Fatalf("ResolveSource: %v", err)
	}
	if id == 0 {
		t.Fatal("expected non-zero source id")
	}
	if tier != manufacturerTier {
		t.Fatalf("tier=%d want %d", tier, manufacturerTier)
	}
	if copyright != "metadata_only" {
		t.Fatalf("copyright=%q want metadata_only", copyright)
	}
	// Source row must have the expected name + source_type.
	var name, sourceType string
	db.QueryRowContext(ctx, `SELECT name, source_type FROM sources WHERE id=?`, id).Scan(&name, &sourceType)
	if name != "Airbus Safety First" {
		t.Fatalf("source name=%q want %q", name, "Airbus Safety First")
	}
	if sourceType != "manufacturer" {
		t.Fatalf("source_type=%q want manufacturer", sourceType)
	}
	// Idempotent: a second doc of the same publication reuses the source.
	doc2ID := seedManufacturerDoc(t, db, "Airbus", "Safety First", "42", "https://s3.example/42.pdf")
	id2, _, _, err := src.ResolveSource(ctx, db, ExtractDoc{ID: doc2ID, SourceRef: "Airbus", ISO2: "airbus"})
	if err != nil {
		t.Fatalf("ResolveSource second: %v", err)
	}
	if id2 != id {
		t.Fatalf("second resolve created new source %d vs %d", id2, id)
	}
}

// ─── MarkSkipped / MarkExtractedTx / RecordFailure / PersistOCRPath ─────────

func TestManufacturerMarkSkipped(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID := seedManufacturerDoc(t, db, "Airbus", "Safety First", "41", "https://s3.example/41.pdf")
	src := ManufacturerSource{HTTP: http.DefaultClient}
	if err := src.MarkSkipped(ctx, db, docID); err != nil {
		t.Fatalf("MarkSkipped: %v", err)
	}
	var status string
	db.QueryRowContext(ctx, `SELECT extraction_status FROM staged_manufacturer_documents WHERE id=?`, docID).Scan(&status)
	if status != "skipped" {
		t.Fatalf("extraction_status=%q want skipped", status)
	}
}

// TestManufacturerRecordFailureNoCrawlErrors verifies the row is marked failed
// with attempts bumped AND that no crawl_errors row is written (manufacturer docs
// have no crawl_job; crawl_errors.crawl_job_id is NOT NULL).
func TestManufacturerRecordFailureNoCrawlErrors(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID := seedManufacturerDoc(t, db, "Airbus", "Safety First", "41", "https://s3.example/41.pdf")

	src := ManufacturerSource{HTTP: http.DefaultClient}
	docs, _ := src.PendingDocs(ctx, db, 0)
	doc := docs[0]
	cause := fmt.Errorf("simulated transport error")
	if err := src.RecordFailure(ctx, db, doc, doc.ArchivedURL, errTypeTransport, cause); err != nil {
		t.Fatalf("RecordFailure: %v", err)
	}
	var status, errMsg string
	var attempts int
	db.QueryRowContext(ctx,
		`SELECT extraction_status, extraction_attempts, coalesce(extraction_error,'') FROM staged_manufacturer_documents WHERE id=?`, docID).
		Scan(&status, &attempts, &errMsg)
	if status != "failed" {
		t.Fatalf("extraction_status=%q want failed", status)
	}
	if attempts != 1 {
		t.Fatalf("extraction_attempts=%d want 1", attempts)
	}
	if errMsg != cause.Error() {
		t.Fatalf("extraction_error=%q want %q", errMsg, cause.Error())
	}
	var n int
	db.QueryRowContext(ctx, `SELECT count(*) FROM crawl_errors`).Scan(&n)
	if n != 0 {
		t.Fatalf("crawl_errors rows=%d want 0 (manufacturer docs have no crawl_job)", n)
	}
}

func TestManufacturerPersistOCRPath(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID := seedManufacturerDoc(t, db, "Airbus", "Safety First", "41", "https://s3.example/41.pdf")
	src := ManufacturerSource{HTTP: http.DefaultClient}
	if err := src.PersistOCRPath(ctx, db, docID, "/tmp/ocr/airbus/abc.txt"); err != nil {
		t.Fatalf("PersistOCRPath: %v", err)
	}
	var ocrPath, status string
	db.QueryRowContext(ctx,
		`SELECT ocr_text_path, extraction_status FROM staged_manufacturer_documents WHERE id=?`, docID).
		Scan(&ocrPath, &status)
	if ocrPath != "/tmp/ocr/airbus/abc.txt" || status != "ocr_done" {
		t.Fatalf("ocr_text_path=%q status=%q", ocrPath, status)
	}
}

// ─── Promotion: NULL country ─────────────────────────────────────────────────

// TestManufacturerPromoteWritesNullCountry is the regression guard for the
// promote.go nullInt64 fix: a country-less manufacturer doc (CountryID==0) must
// promote to an event with occurrence_country_id NULL (not 0, which would violate
// the FK), link the staged row, and write a manufacturer/metadata_only report.
func TestManufacturerPromoteWritesNullCountry(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID := seedManufacturerDoc(t, db, "Airbus", "Safety First", "41", "https://s3.example/41.pdf")

	src := ManufacturerSource{HTTP: http.DefaultClient}
	doc := ExtractDoc{
		ID:          docID,
		CountryID:   0, // global
		ISO2:        "airbus",
		SourceRef:   "Airbus",
		OriginalURL: "https://safetyfirst.example/41",
	}
	e := ExtractedEvent{
		IsAviationAccident:   true,
		Date:                 "2024-03-15",
		DatePrecision:        "exact",
		AircraftRegistration: "F-WXYZ",
		AircraftType:         "A320",
		Manufacturer:         "Airbus",
		EventType:            "accident",
		InvestigationStatus:  "final_report_available",
		ReportType:           "factual",
		Title:                "Safety First case study",
		Language:             "en",
	}
	eventID, linked, err := PromoteDocument(ctx, db, src, doc, e)
	if err != nil {
		t.Fatalf("PromoteDocument: %v", err)
	}
	if linked {
		t.Fatal("expected a new event, not a dedup link")
	}

	var country sql.NullInt64
	db.QueryRowContext(ctx, `SELECT occurrence_country_id FROM events WHERE id=?`, eventID).Scan(&country)
	if country.Valid {
		t.Fatalf("occurrence_country_id must be NULL for a manufacturer doc, got %d", country.Int64)
	}

	var status string
	var ev sql.NullInt64
	db.QueryRowContext(ctx,
		`SELECT extraction_status, event_id FROM staged_manufacturer_documents WHERE id=?`, docID).
		Scan(&status, &ev)
	if status != "extracted" || !ev.Valid || ev.Int64 != eventID {
		t.Fatalf("staged row status=%q event_id=%v want extracted/%d", status, ev, eventID)
	}

	var copyright, sourceType string
	var tier int
	db.QueryRowContext(ctx, `
		SELECT r.copyright_status, r.source_tier, s.source_type
		  FROM reports r JOIN sources s ON s.id = r.source_id
		 WHERE r.event_id=?`, eventID).Scan(&copyright, &tier, &sourceType)
	if copyright != "metadata_only" || tier != manufacturerTier || sourceType != "manufacturer" {
		t.Fatalf("report copyright=%q tier=%d source_type=%q", copyright, tier, sourceType)
	}
}

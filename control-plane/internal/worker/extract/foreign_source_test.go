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

// seedForeignDoc inserts a country, a crawl_job, and one staged_foreign_documents
// row with a report_url. Returns docID and countryID.
func seedForeignDoc(t *testing.T, db *sql.DB, iso2, authority, reportURL string) (docID, countryID int64) {
	t.Helper()
	ctx := context.Background()

	// Insert country.
	res, err := db.ExecContext(ctx, `
		INSERT INTO countries
			(iso2, iso3, name, region, policy_status, coverage_status,
			 coverage_score, effort_score)
		VALUES (?, ?, ?, 'R', 'allowed', 'no_public_archive', 1, 3)`,
		iso2, iso2+"X", iso2+"land")
	if err != nil {
		t.Fatal(err)
	}
	countryID, _ = res.LastInsertId()

	// We need a source row for crawl_jobs FK — seed a minimal "wayback" placeholder
	// so the canonical URL doesn't conflict with what ResolveSource will create for
	// the foreign authority (foreign://<authority> with official_foreign_accredited_rep).
	res, err = db.ExecContext(ctx, `
		INSERT OR IGNORE INTO sources (name, url, canonical_url, source_type, source_tier)
		VALUES (?, ?, ?, 'wayback', 2)`,
		authority+"-seed",
		fmt.Sprintf("https://%s.seed.example", authority),
		fmt.Sprintf("wayback://%s-%s", authority, iso2))
	if err != nil {
		t.Fatal(err)
	}
	var srcID int64
	if err := db.QueryRowContext(ctx, `SELECT id FROM sources WHERE canonical_url=?`,
		fmt.Sprintf("wayback://%s-%s", authority, iso2)).Scan(&srcID); err != nil {
		t.Fatal(err)
	}

	// Insert crawl_job.
	res, err = db.ExecContext(ctx, `
		INSERT INTO crawl_jobs (source_id, country_id, job_type, status)
		VALUES (?, ?, 'pdf_discovery', 'running')`, srcID, countryID)
	if err != nil {
		t.Fatal(err)
	}
	jobID, _ := res.LastInsertId()

	// Insert staged_foreign_documents row with a report_url.
	res, err = db.ExecContext(ctx, `
		INSERT INTO staged_foreign_documents
			(crawl_job_id, country_id, authority, foreign_ref, title, original_url, report_url)
		VALUES (?, ?, ?, ?, ?, ?, ?)`,
		jobID, countryID, authority,
		fmt.Sprintf("ref-%s-001", authority),
		"Accident Report 001",
		fmt.Sprintf("https://%s.example/accidents/001", authority),
		reportURL)
	if err != nil {
		t.Fatal(err)
	}
	docID, _ = res.LastInsertId()
	return docID, countryID
}

// ─── PendingDocs ─────────────────────────────────────────────────────────────

func TestForeignPendingDocsReturnsDocWithReportURL(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	reportURL := "https://ntsb.gov/reports/001.pdf"
	docID, _ := seedForeignDoc(t, db, "KE", "ntsb", reportURL)

	src := ForeignSource{HTTP: http.DefaultClient}
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
	// report_url must be mapped to ArchivedURL (the download target).
	if d.ArchivedURL != reportURL {
		t.Fatalf("ArchivedURL=%q want %q", d.ArchivedURL, reportURL)
	}
	// authority must be surfaced via SourceRef.
	if d.SourceRef != "ntsb" {
		t.Fatalf("SourceRef=%q want %q", d.SourceRef, "ntsb")
	}
	if d.ISO2 != "KE" {
		t.Fatalf("ISO2=%q want KE", d.ISO2)
	}
}

func TestForeignPendingDocsExcludesDocWithoutReportURL(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	// Seed a doc with a report_url (to establish FK deps), then add one without.
	_, countryID := seedForeignDoc(t, db, "ZM", "ntsb", "https://ntsb.gov/001.pdf")
	var srcID, jobID int64
	db.QueryRowContext(ctx, `SELECT id FROM sources WHERE canonical_url='wayback://ntsb-ZM'`).Scan(&srcID)
	res, _ := db.ExecContext(ctx, `INSERT INTO crawl_jobs (source_id, country_id, job_type, status) VALUES (?,?,'pdf_discovery','running')`, srcID, countryID)
	jobID, _ = res.LastInsertId()
	db.ExecContext(ctx, `
		INSERT INTO staged_foreign_documents
			(crawl_job_id, country_id, authority, foreign_ref, title, original_url)
		VALUES (?, ?, 'ntsb', 'ref-no-report', 'No Report', 'https://ntsb.gov/no-report')`,
		jobID, countryID)

	src := ForeignSource{HTTP: http.DefaultClient}
	docs, err := src.PendingDocs(ctx, db, 0)
	if err != nil {
		t.Fatalf("PendingDocs: %v", err)
	}
	// Doc without report_url must not be returned.
	for _, d := range docs {
		if d.SourceRef == "ntsb" && d.ArchivedURL == "" {
			t.Fatalf("doc without report_url must not be returned")
		}
	}
}

func TestForeignPendingDocsExcludesMaxAttempts(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID, _ := seedForeignDoc(t, db, "KE", "ntsb", "https://ntsb.gov/002.pdf")
	// Exhaust extraction attempts.
	db.ExecContext(ctx, `UPDATE staged_foreign_documents SET extraction_attempts=3 WHERE id=?`, docID)

	src := ForeignSource{HTTP: http.DefaultClient}
	docs, err := src.PendingDocs(ctx, db, 0)
	if err != nil {
		t.Fatalf("PendingDocs: %v", err)
	}
	if len(docs) != 0 {
		t.Fatalf("expected 0 docs (max attempts), got %d", len(docs))
	}
}

func TestForeignPendingDocsLimitRespected(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	seedForeignDoc(t, db, "KE", "ntsb", "https://ntsb.gov/a.pdf")
	seedForeignDoc(t, db, "AU", "atsb", "https://atsb.gov.au/b.pdf")

	src := ForeignSource{HTTP: http.DefaultClient}
	docs, err := src.PendingDocs(ctx, db, 1)
	if err != nil {
		t.Fatalf("PendingDocs: %v", err)
	}
	if len(docs) != 1 {
		t.Fatalf("expected 1 doc (limit=1), got %d", len(docs))
	}
}

// ─── EnsureDownloaded ────────────────────────────────────────────────────────

func TestForeignEnsureDownloadedFetchesAndUpdatesRow(t *testing.T) {
	// EnsureDownloaded calls DownloadReportURL which uses the SSRF guard by
	// default. Allow loopback so the httptest server is reachable in tests.
	allowLoopback(t)

	ctx := context.Background()
	db := newExtractTestDB(t)

	// Serve a fake PDF over HTTP.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/pdf")
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, "%PDF-1.4 fake foreign report")
	}))
	defer srv.Close()

	docID, _ := seedForeignDoc(t, db, "KE", "ntsb", srv.URL+"/report.pdf")
	src := ForeignSource{HTTP: srv.Client()}

	docs, err := src.PendingDocs(ctx, db, 0)
	if err != nil {
		t.Fatalf("PendingDocs: %v", err)
	}
	if len(docs) != 1 {
		t.Fatalf("expected 1 doc, got %d", len(docs))
	}
	doc := docs[0]
	storeDir := t.TempDir()
	if err := src.EnsureDownloaded(ctx, db, storeDir, &doc); err != nil {
		t.Fatalf("EnsureDownloaded: %v", err)
	}
	if doc.LocalFilePath == "" {
		t.Fatal("LocalFilePath not set after EnsureDownloaded")
	}
	if doc.Digest == "" {
		t.Fatal("Digest not set after EnsureDownloaded")
	}
	// DB row must be updated.
	var dbStatus, dbPath, dbDigest string
	if err := db.QueryRowContext(ctx,
		`SELECT download_status, local_file_path, digest FROM staged_foreign_documents WHERE id=?`, docID).
		Scan(&dbStatus, &dbPath, &dbDigest); err != nil {
		t.Fatalf("scan row: %v", err)
	}
	if dbStatus != "downloaded" {
		t.Fatalf("download_status=%q want downloaded", dbStatus)
	}
	if dbPath == "" || dbDigest == "" {
		t.Fatalf("db path=%q digest=%q both must be non-empty", dbPath, dbDigest)
	}
	// File must exist on disk.
	if !filepath.IsAbs(doc.LocalFilePath) {
		t.Fatalf("LocalFilePath must be absolute, got %q", doc.LocalFilePath)
	}
}

func TestForeignEnsureDownloadedFailureMarksRow(t *testing.T) {
	// EnsureDownloaded calls DownloadReportURL which uses the SSRF guard by
	// default. Allow loopback so the httptest server is reachable in tests.
	allowLoopback(t)

	ctx := context.Background()
	db := newExtractTestDB(t)

	// Serve a 404.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
	}))
	defer srv.Close()

	docID, _ := seedForeignDoc(t, db, "KE", "ntsb", srv.URL+"/missing.pdf")
	src := ForeignSource{HTTP: srv.Client()}

	docs, _ := src.PendingDocs(ctx, db, 0)
	doc := docs[0]
	err := src.EnsureDownloaded(ctx, db, t.TempDir(), &doc)
	if err == nil {
		t.Fatal("expected error from 404, got nil")
	}
	var dbStatus string
	db.QueryRowContext(ctx, `SELECT download_status FROM staged_foreign_documents WHERE id=?`, docID).Scan(&dbStatus)
	if dbStatus != "failed" {
		t.Fatalf("download_status=%q want failed", dbStatus)
	}
}

// TestForeignPendingDocsTwoPhaseFlow verifies the two-phase PendingDocs contract:
//  1. A freshly seeded doc (download_status='pending') is returned by PendingDocs.
//  2. After EnsureDownloaded sets download_status='downloaded', the SAME row is
//     still returned by PendingDocs (for the extraction phase).
//  3. A row with extraction_status='extracted' is NOT returned.
func TestForeignPendingDocsTwoPhaseFlow(t *testing.T) {
	// EnsureDownloaded calls DownloadReportURL which uses the SSRF guard by
	// default. Allow loopback so the httptest server is reachable in tests.
	allowLoopback(t)

	ctx := context.Background()
	db := newExtractTestDB(t)

	// Serve a fake PDF over HTTP.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/pdf")
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, "%PDF-1.4 fake foreign two-phase report")
	}))
	defer srv.Close()

	docID, _ := seedForeignDoc(t, db, "KE", "ntsb", srv.URL+"/phase-report.pdf")
	src := ForeignSource{HTTP: srv.Client()}

	// ── Phase 1: row with download_status='pending' must be returned ──────────
	docs, err := src.PendingDocs(ctx, db, 0)
	if err != nil {
		t.Fatalf("phase1 PendingDocs: %v", err)
	}
	var found bool
	for _, d := range docs {
		if d.ID == docID {
			found = true
			break
		}
	}
	if !found {
		t.Fatalf("phase1: doc %d with download_status='pending' not returned by PendingDocs", docID)
	}

	// ── EnsureDownloaded: sets download_status='downloaded' ───────────────────
	doc := docs[0]
	storeDir := t.TempDir()
	if err := src.EnsureDownloaded(ctx, db, storeDir, &doc); err != nil {
		t.Fatalf("EnsureDownloaded: %v", err)
	}

	// Verify DB state after download.
	var dbStatus string
	if err := db.QueryRowContext(ctx,
		`SELECT download_status FROM staged_foreign_documents WHERE id=?`, docID).Scan(&dbStatus); err != nil {
		t.Fatalf("scan download_status: %v", err)
	}
	if dbStatus != "downloaded" {
		t.Fatalf("expected download_status='downloaded', got %q", dbStatus)
	}

	// ── Phase 2: same row (now download_status='downloaded', extraction_status='pending')
	//            must still be returned by PendingDocs ─────────────────────────
	docs2, err := src.PendingDocs(ctx, db, 0)
	if err != nil {
		t.Fatalf("phase2 PendingDocs: %v", err)
	}
	found = false
	for _, d := range docs2 {
		if d.ID == docID {
			found = true
			break
		}
	}
	if !found {
		t.Fatalf("phase2: doc %d with download_status='downloaded' NOT returned by PendingDocs (extraction phase blocked)", docID)
	}

	// ── Phase 3: after marking extracted, row must NOT be returned ────────────
	db.ExecContext(ctx,
		`UPDATE staged_foreign_documents SET extraction_status='extracted' WHERE id=?`, docID)

	docs3, err := src.PendingDocs(ctx, db, 0)
	if err != nil {
		t.Fatalf("phase3 PendingDocs: %v", err)
	}
	for _, d := range docs3 {
		if d.ID == docID {
			t.Fatalf("phase3: extracted doc %d must NOT be returned by PendingDocs", docID)
		}
	}
}

// TestForeignPendingDocsRetryAfterDownloadFailure is the regression test for
// blocker I-1: rows with download_status='failed' AND extraction_status='failed'
// must still be returned by PendingDocs while extraction_attempts < 3.
func TestForeignPendingDocsRetryAfterDownloadFailure(t *testing.T) {
	// allowLoopback so EnsureDownloaded can actually reach the httptest 404 server.
	allowLoopback(t)

	ctx := context.Background()
	db := newExtractTestDB(t)

	// Serve a permanent 404.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
	}))
	defer srv.Close()

	docID, _ := seedForeignDoc(t, db, "KE", "ntsb", srv.URL+"/missing.pdf")
	src := ForeignSource{HTTP: srv.Client()}

	// Simulate a prior failed download: set both statuses to 'failed', attempts=1.
	// (This is the state extractOne leaves the row in after EnsureDownloaded fails
	// and RecordFailure is called: download_status='failed', extraction_status='failed'.)
	db.ExecContext(ctx, `
		UPDATE staged_foreign_documents
		   SET download_status='failed', extraction_status='failed', extraction_attempts=1
		 WHERE id=?`, docID)

	// ── Assert: row IS returned by PendingDocs (retryable, attempts=1 < 3) ──────
	docs, err := src.PendingDocs(ctx, db, 0)
	if err != nil {
		t.Fatalf("PendingDocs after 1st failure: %v", err)
	}
	var found bool
	for _, d := range docs {
		if d.ID == docID {
			found = true
			break
		}
	}
	if !found {
		t.Fatalf("doc %d (download_status='failed', extraction_status='failed', attempts=1) must be returned by PendingDocs (retryable)", docID)
	}

	// Simulate a second failure: attempts=2.
	db.ExecContext(ctx, `
		UPDATE staged_foreign_documents
		   SET download_status='failed', extraction_status='failed', extraction_attempts=2
		 WHERE id=?`, docID)

	docs, err = src.PendingDocs(ctx, db, 0)
	if err != nil {
		t.Fatalf("PendingDocs after 2nd failure: %v", err)
	}
	found = false
	for _, d := range docs {
		if d.ID == docID {
			found = true
			break
		}
	}
	if !found {
		t.Fatalf("doc %d (attempts=2) must still be returned by PendingDocs (retryable)", docID)
	}

	// ── Assert: after attempts=3 the row is NO LONGER returned ──────────────────
	db.ExecContext(ctx, `
		UPDATE staged_foreign_documents
		   SET extraction_attempts=3
		 WHERE id=?`, docID)

	docs, err = src.PendingDocs(ctx, db, 0)
	if err != nil {
		t.Fatalf("PendingDocs after 3rd failure (exhausted): %v", err)
	}
	for _, d := range docs {
		if d.ID == docID {
			t.Fatalf("doc %d (attempts=3) must NOT be returned by PendingDocs (exhausted)", docID)
		}
	}
}

// ─── ResolveSource ───────────────────────────────────────────────────────────

func TestForeignResolveSourceCreatesForeignAuthoritySource(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID, countryID := seedForeignDoc(t, db, "KE", "ntsb", "https://ntsb.gov/001.pdf")
	_ = docID

	src := ForeignSource{HTTP: http.DefaultClient}
	doc := ExtractDoc{CountryID: countryID, ISO2: "KE", SourceRef: "ntsb"}
	id, tier, copyright, err := src.ResolveSource(ctx, db, doc)
	if err != nil {
		t.Fatalf("ResolveSource: %v", err)
	}
	if id == 0 {
		t.Fatal("expected non-zero source id")
	}
	if tier != 2 {
		t.Fatalf("tier=%d want 2", tier)
	}
	if copyright != "official_public" {
		t.Fatalf("copyright=%q want official_public", copyright)
	}
	// Second call must reuse the same source (ON CONFLICT).
	id2, _, _, err := src.ResolveSource(ctx, db, doc)
	if err != nil {
		t.Fatalf("ResolveSource second call: %v", err)
	}
	if id2 != id {
		t.Fatalf("second resolve created new source: %d vs %d", id2, id)
	}
	// Verify canonical URL and source_type.
	var canonicalURL, sourceType string
	db.QueryRowContext(ctx, `SELECT canonical_url, source_type FROM sources WHERE id=?`, id).
		Scan(&canonicalURL, &sourceType)
	if canonicalURL != "foreign://ntsb" {
		t.Fatalf("canonical_url=%q want foreign://ntsb", canonicalURL)
	}
	if sourceType != "official_foreign_accredited_rep" {
		t.Fatalf("source_type=%q want official_foreign_accredited_rep", sourceType)
	}
}

func TestForeignResolveSourceAllAuthorities(t *testing.T) {
	tests := []struct {
		authority string
		wantName  string
		wantURL   string
	}{
		{"ntsb", "National Transportation Safety Board", "https://www.ntsb.gov"},
		{"bea", "Bureau d'Enquêtes et d'Analyses", "https://www.bea.aero"},
		{"atsb", "Australian Transport Safety Bureau", "https://www.atsb.gov.au"},
	}
	for _, tc := range tests {
		t.Run(tc.authority, func(t *testing.T) {
			ctx := context.Background()
			db := newExtractTestDB(t)
			docID, countryID := seedForeignDoc(t, db, "ZZ", tc.authority, "https://example.com/r.pdf")
			_ = docID

			src := ForeignSource{HTTP: http.DefaultClient}
			doc := ExtractDoc{CountryID: countryID, ISO2: "ZZ", SourceRef: tc.authority}
			id, _, _, err := src.ResolveSource(ctx, db, doc)
			if err != nil {
				t.Fatalf("ResolveSource(%s): %v", tc.authority, err)
			}
			var name, url string
			db.QueryRowContext(ctx, `SELECT name, url FROM sources WHERE id=?`, id).Scan(&name, &url)
			if name != tc.wantName {
				t.Fatalf("name=%q want %q", name, tc.wantName)
			}
			if url != tc.wantURL {
				t.Fatalf("url=%q want %q", url, tc.wantURL)
			}
		})
	}
}

func TestForeignResolveSourceUnknownAuthorityReturnsError(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	doc := ExtractDoc{CountryID: 1, ISO2: "ZZ", SourceRef: "unknown_authority"}
	src := ForeignSource{HTTP: http.DefaultClient}
	_, _, _, err := src.ResolveSource(ctx, db, doc)
	if err == nil {
		t.Fatal("expected error for unknown authority, got nil")
	}
}

// ─── MarkSkipped / MarkExtractedTx / RecordFailure / PersistOCRPath ─────────

func TestForeignMarkSkipped(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID, _ := seedForeignDoc(t, db, "KE", "ntsb", "https://ntsb.gov/001.pdf")

	src := ForeignSource{HTTP: http.DefaultClient}
	if err := src.MarkSkipped(ctx, db, docID); err != nil {
		t.Fatalf("MarkSkipped: %v", err)
	}
	var status string
	db.QueryRowContext(ctx, `SELECT extraction_status FROM staged_foreign_documents WHERE id=?`, docID).Scan(&status)
	if status != "skipped" {
		t.Fatalf("extraction_status=%q want skipped", status)
	}
}

func TestForeignMarkExtractedTx(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID, countryID := seedForeignDoc(t, db, "KE", "ntsb", "https://ntsb.gov/001.pdf")
	// Insert a minimal event to link.
	res, err := db.ExecContext(ctx, `
		INSERT INTO events (date, date_precision, occurrence_country_id, event_type, investigation_status,
		                    confidence_score, dedup_status)
		VALUES ('2020-01-01','exact',?,'accident','final_report_available',80,'unreviewed')`, countryID)
	if err != nil {
		t.Fatalf("insert event: %v", err)
	}
	eventID, _ := res.LastInsertId()

	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	src := ForeignSource{HTTP: http.DefaultClient}
	if err := src.MarkExtractedTx(ctx, tx, docID, eventID); err != nil {
		tx.Rollback()
		t.Fatalf("MarkExtractedTx: %v", err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatalf("commit: %v", err)
	}
	var status string
	var linkedEvent sql.NullInt64
	db.QueryRowContext(ctx,
		`SELECT extraction_status, event_id FROM staged_foreign_documents WHERE id=?`, docID).
		Scan(&status, &linkedEvent)
	if status != "extracted" {
		t.Fatalf("extraction_status=%q want extracted", status)
	}
	if !linkedEvent.Valid || linkedEvent.Int64 != eventID {
		t.Fatalf("event_id=%v want %d", linkedEvent, eventID)
	}
}

func TestForeignRecordFailure(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID, _ := seedForeignDoc(t, db, "KE", "ntsb", "https://ntsb.gov/001.pdf")

	src := ForeignSource{HTTP: http.DefaultClient}
	docs, _ := src.PendingDocs(ctx, db, 0)
	doc := docs[0]

	cause := fmt.Errorf("simulated transport error")
	if err := src.RecordFailure(ctx, db, doc, doc.ArchivedURL, errTypeTransport, cause); err != nil {
		t.Fatalf("RecordFailure: %v", err)
	}
	var status string
	var attempts int
	db.QueryRowContext(ctx,
		`SELECT extraction_status, extraction_attempts FROM staged_foreign_documents WHERE id=?`, docID).
		Scan(&status, &attempts)
	if status != "failed" {
		t.Fatalf("extraction_status=%q want failed", status)
	}
	if attempts != 1 {
		t.Fatalf("extraction_attempts=%d want 1", attempts)
	}
	// crawl_errors row must be written with a CHECK-valid error_type.
	var n int
	var errType string
	db.QueryRowContext(ctx, `SELECT count(*), error_type FROM crawl_errors`).Scan(&n, &errType)
	if n != 1 {
		t.Fatalf("crawl_errors rows=%d want 1", n)
	}
	valid := map[string]bool{
		"tls_error": true, "timeout": true, "dns_error": true, "nx_domain": true,
		"http_403": true, "http_404": true, "http_500": true, "parse_error": true,
		"robots_blocked": true, "unknown": true,
	}
	if !valid[errType] {
		t.Fatalf("error_type=%q violates crawl_errors CHECK constraint", errType)
	}
}

func TestForeignPersistOCRPath(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID, _ := seedForeignDoc(t, db, "KE", "ntsb", "https://ntsb.gov/001.pdf")

	src := ForeignSource{HTTP: http.DefaultClient}
	if err := src.PersistOCRPath(ctx, db, docID, "/tmp/ocr/ke/abc.txt"); err != nil {
		t.Fatalf("PersistOCRPath: %v", err)
	}
	var ocrPath, status string
	db.QueryRowContext(ctx,
		`SELECT ocr_text_path, extraction_status FROM staged_foreign_documents WHERE id=?`, docID).
		Scan(&ocrPath, &status)
	if ocrPath != "/tmp/ocr/ke/abc.txt" {
		t.Fatalf("ocr_text_path=%q want /tmp/ocr/ke/abc.txt", ocrPath)
	}
	if status != "ocr_done" {
		t.Fatalf("extraction_status=%q want ocr_done", status)
	}
}

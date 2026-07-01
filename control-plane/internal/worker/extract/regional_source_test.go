package extract

import (
	"context"
	"database/sql"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// seedRegionalDoc inserts a country, a regional_body, a crawl_job, and one
// staged_regional_documents row with a report_url. Returns docID and countryID.
func seedRegionalDoc(t *testing.T, db *sql.DB, iso2, bodyCode, reportURL string) (docID, countryID int64) {
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

	// Insert regional_body.
	res, err = db.ExecContext(ctx, `
		INSERT OR IGNORE INTO regional_bodies (code, name, body_class, website_url, source_url)
		VALUES (?, ?, 'regional_body', ?, ?)`,
		bodyCode, bodyCode+" Body", fmt.Sprintf("https://%s.example", bodyCode),
		fmt.Sprintf("https://%s.example/reports", bodyCode))
	if err != nil {
		t.Fatal(err)
	}

	// Get or find the regional_body id for crawl_job source.
	var bodyID int64
	if err := db.QueryRowContext(ctx, `SELECT id FROM regional_bodies WHERE code=?`, bodyCode).Scan(&bodyID); err != nil {
		t.Fatal(err)
	}

	// We need a source row for crawl_jobs FK — seed a minimal one.
	res, err = db.ExecContext(ctx, `
		INSERT OR IGNORE INTO sources (name, url, canonical_url, source_type, source_tier)
		VALUES (?, ?, ?, 'regional_body', 4)`,
		bodyCode, fmt.Sprintf("https://%s.example", bodyCode),
		fmt.Sprintf("regional://%s", bodyCode))
	if err != nil {
		t.Fatal(err)
	}
	var srcID int64
	if err := db.QueryRowContext(ctx, `SELECT id FROM sources WHERE canonical_url=?`,
		fmt.Sprintf("regional://%s", bodyCode)).Scan(&srcID); err != nil {
		t.Fatal(err)
	}

	// Insert crawl_job. Use 'pdf_discovery' — regional has no distinct job_type in the CHECK.
	res, err = db.ExecContext(ctx, `
		INSERT INTO crawl_jobs (source_id, country_id, job_type, status)
		VALUES (?, ?, 'pdf_discovery', 'running')`, srcID, countryID)
	if err != nil {
		t.Fatal(err)
	}
	jobID, _ := res.LastInsertId()

	// Insert staged_regional_documents row with a report_url.
	res, err = db.ExecContext(ctx, `
		INSERT INTO staged_regional_documents
			(crawl_job_id, country_id, body_code, ref, title, original_url, report_url)
		VALUES (?, ?, ?, ?, ?, ?, ?)`,
		jobID, countryID, bodyCode,
		fmt.Sprintf("ref-%s-001", bodyCode),
		"Accident Report 001",
		fmt.Sprintf("https://%s.example/accidents/001", bodyCode),
		reportURL)
	if err != nil {
		t.Fatal(err)
	}
	docID, _ = res.LastInsertId()
	return docID, countryID
}

// ─── PendingDocs ─────────────────────────────────────────────────────────────

func TestRegionalPendingDocsReturnsDocWithReportURL(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	reportURL := "https://eccaa.example/reports/001.pdf"
	docID, _ := seedRegionalDoc(t, db, "TZ", "ECCAA", reportURL)

	src := RegionalSource{HTTP: http.DefaultClient}
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
	// body_code must be surfaced via SourceRef.
	if d.SourceRef != "ECCAA" {
		t.Fatalf("SourceRef=%q want %q", d.SourceRef, "ECCAA")
	}
	if d.ISO2 != "TZ" {
		t.Fatalf("ISO2=%q want TZ", d.ISO2)
	}
}

// TestRegionalPendingDocsIncludesHtmlOnlyDoc verifies the new behaviour: a doc
// with original_url set and report_url NULL (html-page/IAC pattern) IS returned
// by PendingDocs. The schema mandates original_url NOT NULL so every row has it;
// what distinguishes pdf vs html is whether report_url is populated.
func TestRegionalPendingDocsIncludesHtmlOnlyDoc(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	// Seed the PDF doc (report_url set).
	pdfDocID, countryID := seedRegionalDoc(t, db, "KE", "ECCAA", "https://eccaa.example/001.pdf")
	// Insert an html-only doc (report_url NULL, original_url set).
	var srcID, jobID int64
	db.QueryRowContext(ctx, `SELECT id FROM sources WHERE canonical_url='regional://ECCAA'`).Scan(&srcID)
	res, _ := db.ExecContext(ctx, `INSERT INTO crawl_jobs (source_id, country_id, job_type, status) VALUES (?,?,'pdf_discovery','running')`, srcID, countryID)
	jobID, _ = res.LastInsertId()
	r2, err := db.ExecContext(ctx, `
		INSERT INTO staged_regional_documents
			(crawl_job_id, country_id, body_code, ref, title, original_url)
		VALUES (?, ?, 'ECCAA', 'ref-html-only', 'HTML Only Doc', 'https://eccaa.example/html-report')`,
		jobID, countryID)
	if err != nil {
		t.Fatalf("insert html doc: %v", err)
	}
	htmlDocID, _ := r2.LastInsertId()

	src := RegionalSource{HTTP: http.DefaultClient}
	docs, err := src.PendingDocs(ctx, db, 0)
	if err != nil {
		t.Fatalf("PendingDocs: %v", err)
	}
	// Both docs must be returned.
	found := map[int64]bool{}
	for _, d := range docs {
		found[d.ID] = true
	}
	if !found[pdfDocID] {
		t.Fatalf("pdf doc %d must be returned", pdfDocID)
	}
	if !found[htmlDocID] {
		t.Fatalf("html-only doc %d (report_url=NULL, original_url set) must be returned", htmlDocID)
	}
}

func TestRegionalPendingDocsExcludesMaxAttempts(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID, _ := seedRegionalDoc(t, db, "TZ", "ECCAA", "https://eccaa.example/002.pdf")
	// Exhaust extraction attempts.
	db.ExecContext(ctx, `UPDATE staged_regional_documents SET extraction_attempts=3 WHERE id=?`, docID)

	src := RegionalSource{HTTP: http.DefaultClient}
	docs, err := src.PendingDocs(ctx, db, 0)
	if err != nil {
		t.Fatalf("PendingDocs: %v", err)
	}
	if len(docs) != 0 {
		t.Fatalf("expected 0 docs (max attempts), got %d", len(docs))
	}
}

func TestRegionalPendingDocsLimitRespected(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	seedRegionalDoc(t, db, "TZ", "ECCAA", "https://eccaa.example/a.pdf")

	// Seed a second country + doc under BAGAIA.
	_, err := db.ExecContext(ctx, `
		INSERT OR IGNORE INTO regional_bodies (code, name, body_class, source_url)
		VALUES ('BAGAIA', 'BAGAIA Body', 'regional_body', 'https://bagaia.example/reports')`)
	if err != nil {
		t.Fatal(err)
	}
	seedRegionalDoc(t, db, "GH", "BAGAIA", "https://bagaia.example/b.pdf")

	src := RegionalSource{HTTP: http.DefaultClient}
	docs, err := src.PendingDocs(ctx, db, 1)
	if err != nil {
		t.Fatalf("PendingDocs: %v", err)
	}
	if len(docs) != 1 {
		t.Fatalf("expected 1 doc (limit=1), got %d", len(docs))
	}
}

// seedRegionalDocNoCountry inserts a staged_regional_documents row with
// country_id NULL — the GO-CP-1 body-wide-listing case (ECCAA/BAGAIA/IAC stage
// every record this way; see regional/stage.go). The owning crawl_job still
// carries a real country (crawl_jobs.country_id is NOT NULL — it identifies
// which job ran the body-wide fetch, not the record's occurrence country).
func seedRegionalDocNoCountry(t *testing.T, db *sql.DB, jobCountryISO2, bodyCode, reportURL string) (docID int64) {
	t.Helper()
	ctx := context.Background()

	res, err := db.ExecContext(ctx, `
		INSERT INTO countries
			(iso2, iso3, name, region, policy_status, coverage_status,
			 coverage_score, effort_score)
		VALUES (?, ?, ?, 'R', 'allowed', 'no_public_archive', 1, 3)`,
		jobCountryISO2, jobCountryISO2+"X", jobCountryISO2+"land")
	if err != nil {
		t.Fatal(err)
	}
	jobCountryID, _ := res.LastInsertId()

	if _, err := db.ExecContext(ctx, `
		INSERT OR IGNORE INTO regional_bodies (code, name, body_class, website_url, source_url)
		VALUES (?, ?, 'regional_body', ?, ?)`,
		bodyCode, bodyCode+" Body", fmt.Sprintf("https://%s.example", bodyCode),
		fmt.Sprintf("https://%s.example/reports", bodyCode)); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(ctx, `
		INSERT OR IGNORE INTO sources (name, url, canonical_url, source_type, source_tier)
		VALUES (?, ?, ?, 'regional_body', 4)`,
		bodyCode, fmt.Sprintf("https://%s.example", bodyCode),
		fmt.Sprintf("regional://%s", bodyCode)); err != nil {
		t.Fatal(err)
	}
	var srcID int64
	if err := db.QueryRowContext(ctx, `SELECT id FROM sources WHERE canonical_url=?`,
		fmt.Sprintf("regional://%s", bodyCode)).Scan(&srcID); err != nil {
		t.Fatal(err)
	}

	res, err = db.ExecContext(ctx, `
		INSERT INTO crawl_jobs (source_id, country_id, job_type, status)
		VALUES (?, ?, 'pdf_discovery', 'running')`, srcID, jobCountryID)
	if err != nil {
		t.Fatal(err)
	}
	jobID, _ := res.LastInsertId()

	// country_id NULL: the GO-CP-1 fix — a body-wide record stages with no
	// country claim, even though its owning job has a real country above.
	res, err = db.ExecContext(ctx, `
		INSERT INTO staged_regional_documents
			(crawl_job_id, country_id, body_code, ref, title, original_url, report_url)
		VALUES (?, NULL, ?, ?, ?, ?, ?)`,
		jobID, bodyCode,
		fmt.Sprintf("ref-%s-nocountry", bodyCode),
		"Unattributed Accident Report",
		fmt.Sprintf("https://%s.example/accidents/nocountry", bodyCode),
		reportURL)
	if err != nil {
		t.Fatal(err)
	}
	docID, _ = res.LastInsertId()
	return docID
}

// TestRegionalPendingDocsIncludesCountryLessRow is the GO-CP-1 regression test
// for the extract queue: before the fix, PendingDocs INNER JOINed countries,
// so a row with country_id NULL (every body-wide record, post-fix) would be
// silently dropped from the extract queue forever — the row would never reach
// OCR/LLM/promotion at all. It must be returned, with a usable ISO2 (falls
// back to the body code) and a usable Priority (falls back to a fixed
// constant) so downstream sorting and store-dir placement still work.
func TestRegionalPendingDocsIncludesCountryLessRow(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID := seedRegionalDocNoCountry(t, db, "BY", "IAC", "https://iac.example/nocountry.pdf")

	src := RegionalSource{HTTP: http.DefaultClient}
	docs, err := src.PendingDocs(ctx, db, 0)
	if err != nil {
		t.Fatalf("PendingDocs: %v", err)
	}
	var found *ExtractDoc
	for i := range docs {
		if docs[i].ID == docID {
			found = &docs[i]
			break
		}
	}
	if found == nil {
		t.Fatalf("country-less doc %d must be returned by PendingDocs (was silently dropped before GO-CP-1 fix)", docID)
	}
	if found.CountryID != 0 {
		t.Fatalf("CountryID=%d want 0 (NULL country_id, not the owning job's country)", found.CountryID)
	}
	if found.ISO2 != "iac" {
		t.Fatalf("ISO2=%q want %q (fallback to lower-cased body_code)", found.ISO2, "iac")
	}
	if found.Priority != regionalUnattributedPriority {
		t.Fatalf("Priority=%v want %v (fallback constant)", found.Priority, regionalUnattributedPriority)
	}
}

// ─── EnsureDownloaded ────────────────────────────────────────────────────────

func TestRegionalEnsureDownloadedFetchesAndUpdatesRow(t *testing.T) {
	// EnsureDownloaded calls DownloadReportURL which uses the SSRF guard by
	// default. Allow loopback so the httptest server is reachable in tests.
	allowLoopback(t)

	ctx := context.Background()
	db := newExtractTestDB(t)

	// Serve a fake PDF over HTTP.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/pdf")
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, "%PDF-1.4 fake regional report")
	}))
	defer srv.Close()

	docID, _ := seedRegionalDoc(t, db, "TZ", "ECCAA", srv.URL+"/report.pdf")
	src := RegionalSource{HTTP: srv.Client()}

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
		`SELECT download_status, local_file_path, digest FROM staged_regional_documents WHERE id=?`, docID).
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

func TestRegionalEnsureDownloadedFailureMarksRow(t *testing.T) {
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

	docID, _ := seedRegionalDoc(t, db, "TZ", "ECCAA", srv.URL+"/missing.pdf")
	src := RegionalSource{HTTP: srv.Client()}

	docs, _ := src.PendingDocs(ctx, db, 0)
	doc := docs[0]
	err := src.EnsureDownloaded(ctx, db, t.TempDir(), &doc)
	if err == nil {
		t.Fatal("expected error from 404, got nil")
	}
	var dbStatus string
	db.QueryRowContext(ctx, `SELECT download_status FROM staged_regional_documents WHERE id=?`, docID).Scan(&dbStatus)
	if dbStatus != "failed" {
		t.Fatalf("download_status=%q want failed", dbStatus)
	}
}

// TestRegionalPendingDocsTwoPhaseFlow verifies the two-phase PendingDocs contract:
//  1. A freshly seeded doc (download_status='pending') is returned by PendingDocs.
//  2. After EnsureDownloaded sets download_status='downloaded', the SAME row is
//     still returned by PendingDocs (for the extraction phase).
//  3. A row with extraction_status='extracted' is NOT returned.
func TestRegionalPendingDocsTwoPhaseFlow(t *testing.T) {
	// EnsureDownloaded calls DownloadReportURL which uses the SSRF guard by
	// default. Allow loopback so the httptest server is reachable in tests.
	allowLoopback(t)

	ctx := context.Background()
	db := newExtractTestDB(t)

	// Serve a fake PDF over HTTP.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/pdf")
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, "%PDF-1.4 fake regional two-phase report")
	}))
	defer srv.Close()

	docID, _ := seedRegionalDoc(t, db, "TZ", "ECCAA", srv.URL+"/phase-report.pdf")
	src := RegionalSource{HTTP: srv.Client()}

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
		`SELECT download_status FROM staged_regional_documents WHERE id=?`, docID).Scan(&dbStatus); err != nil {
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
		`UPDATE staged_regional_documents SET extraction_status='extracted' WHERE id=?`, docID)

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

// TestRegionalPendingDocsRetryAfterDownloadFailure is the regression test for
// blocker I-1: rows with download_status='failed' AND extraction_status='failed'
// must still be returned by PendingDocs while extraction_attempts < 3.
func TestRegionalPendingDocsRetryAfterDownloadFailure(t *testing.T) {
	// allowLoopback so EnsureDownloaded can actually reach the httptest 404 server.
	allowLoopback(t)

	ctx := context.Background()
	db := newExtractTestDB(t)

	// Serve a permanent 404.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
	}))
	defer srv.Close()

	docID, _ := seedRegionalDoc(t, db, "TZ", "ECCAA", srv.URL+"/missing.pdf")
	src := RegionalSource{HTTP: srv.Client()}

	// Simulate a prior failed download: set both statuses to 'failed', attempts=1.
	// (This is the state extractOne leaves the row in after EnsureDownloaded fails
	// and RecordFailure is called: download_status='failed', extraction_status='failed'.)
	db.ExecContext(ctx, `
		UPDATE staged_regional_documents
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
		UPDATE staged_regional_documents
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
		UPDATE staged_regional_documents
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

func TestRegionalResolveSourceCreatesRegionalBodySource(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID, countryID := seedRegionalDoc(t, db, "TZ", "ECCAA", "https://eccaa.example/001.pdf")
	_ = docID

	src := RegionalSource{HTTP: http.DefaultClient}
	doc := ExtractDoc{CountryID: countryID, ISO2: "TZ", SourceRef: "ECCAA"}
	id, tier, copyright, err := src.ResolveSource(ctx, db, doc)
	if err != nil {
		t.Fatalf("ResolveSource: %v", err)
	}
	if id == 0 {
		t.Fatal("expected non-zero source id")
	}
	if tier != 4 {
		t.Fatalf("tier=%d want 4", tier)
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
	var n int
	db.QueryRowContext(ctx, `SELECT count(*) FROM sources WHERE source_type='regional_body'`).Scan(&n)
	// The seed already inserted one; we should still have exactly 1 distinct ECCAA source.
	// (ON CONFLICT DO NOTHING means no duplicates.)
	var eccaaN int
	db.QueryRowContext(ctx, `SELECT count(*) FROM sources WHERE source_type='regional_body' AND canonical_url LIKE '%ECCAA%'`).Scan(&eccaaN)
	if eccaaN != 1 {
		t.Fatalf("expected 1 ECCAA regional_body source, got %d", eccaaN)
	}
}

func TestRegionalResolveSourceUnknownBodyReturnsError(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	// No regional_body row for "FAKE".
	doc := ExtractDoc{CountryID: 1, ISO2: "ZZ", SourceRef: "FAKE"}
	src := RegionalSource{HTTP: http.DefaultClient}
	_, _, _, err := src.ResolveSource(ctx, db, doc)
	if err == nil {
		t.Fatal("expected error for unknown body code, got nil")
	}
}

// ─── MarkSkipped / MarkExtractedTx / RecordFailure / PersistOCRPath ─────────

func TestRegionalMarkSkipped(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID, _ := seedRegionalDoc(t, db, "TZ", "ECCAA", "https://eccaa.example/001.pdf")

	src := RegionalSource{HTTP: http.DefaultClient}
	if err := src.MarkSkipped(ctx, db, docID); err != nil {
		t.Fatalf("MarkSkipped: %v", err)
	}
	var status string
	db.QueryRowContext(ctx, `SELECT extraction_status FROM staged_regional_documents WHERE id=?`, docID).Scan(&status)
	if status != "skipped" {
		t.Fatalf("extraction_status=%q want skipped", status)
	}
}

func TestRegionalMarkExtractedTx(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID, countryID := seedRegionalDoc(t, db, "TZ", "ECCAA", "https://eccaa.example/001.pdf")
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
	src := RegionalSource{HTTP: http.DefaultClient}
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
		`SELECT extraction_status, event_id FROM staged_regional_documents WHERE id=?`, docID).
		Scan(&status, &linkedEvent)
	if status != "extracted" {
		t.Fatalf("extraction_status=%q want extracted", status)
	}
	if !linkedEvent.Valid || linkedEvent.Int64 != eventID {
		t.Fatalf("event_id=%v want %d", linkedEvent, eventID)
	}
}

func TestRegionalRecordFailure(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID, _ := seedRegionalDoc(t, db, "TZ", "ECCAA", "https://eccaa.example/001.pdf")

	src := RegionalSource{HTTP: http.DefaultClient}
	docs, _ := src.PendingDocs(ctx, db, 0)
	doc := docs[0]

	cause := fmt.Errorf("simulated transport error")
	if err := src.RecordFailure(ctx, db, doc, doc.ArchivedURL, errTypeTransport, cause); err != nil {
		t.Fatalf("RecordFailure: %v", err)
	}
	var status string
	var attempts int
	db.QueryRowContext(ctx,
		`SELECT extraction_status, extraction_attempts FROM staged_regional_documents WHERE id=?`, docID).
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

func TestRegionalPersistOCRPath(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID, _ := seedRegionalDoc(t, db, "TZ", "ECCAA", "https://eccaa.example/001.pdf")

	src := RegionalSource{HTTP: http.DefaultClient}
	if err := src.PersistOCRPath(ctx, db, docID, "/tmp/ocr/tz/abc.txt"); err != nil {
		t.Fatalf("PersistOCRPath: %v", err)
	}
	var ocrPath, status string
	db.QueryRowContext(ctx,
		`SELECT ocr_text_path, extraction_status FROM staged_regional_documents WHERE id=?`, docID).
		Scan(&ocrPath, &status)
	if ocrPath != "/tmp/ocr/tz/abc.txt" {
		t.Fatalf("ocr_text_path=%q want /tmp/ocr/tz/abc.txt", ocrPath)
	}
	if status != "ocr_done" {
		t.Fatalf("extraction_status=%q want ocr_done", status)
	}
}

// ─── IAC html-page support (no report_url, original_url only) ────────────────

// seedRegionalHtmlDoc inserts a staged_regional_documents row with original_url
// set and report_url NULL — the IAC html-page pattern.
func seedRegionalHtmlDoc(t *testing.T, db *sql.DB, iso2, bodyCode, originalURL string) (docID, countryID int64) {
	t.Helper()
	ctx := context.Background()

	res, err := db.ExecContext(ctx, `
		INSERT INTO countries
			(iso2, iso3, name, region, policy_status, coverage_status,
			 coverage_score, effort_score)
		VALUES (?, ?, ?, 'R', 'allowed', 'no_public_archive', 1, 3)`,
		iso2, iso2+"Y", iso2+"htmland")
	if err != nil {
		t.Fatal(err)
	}
	countryID, _ = res.LastInsertId()

	_, err = db.ExecContext(ctx, `
		INSERT OR IGNORE INTO regional_bodies (code, name, body_class, website_url, source_url)
		VALUES (?, ?, 'regional_body', ?, ?)`,
		bodyCode, bodyCode+" Body HTML",
		fmt.Sprintf("https://%s.html.example", bodyCode),
		fmt.Sprintf("https://%s.html.example/reports", bodyCode))
	if err != nil {
		t.Fatal(err)
	}

	res, err = db.ExecContext(ctx, `
		INSERT OR IGNORE INTO sources (name, url, canonical_url, source_type, source_tier)
		VALUES (?, ?, ?, 'regional_body', 4)`,
		bodyCode+"html", fmt.Sprintf("https://%s.html.example", bodyCode),
		fmt.Sprintf("regional://%s", bodyCode))
	if err != nil {
		t.Fatal(err)
	}
	var srcID int64
	if err := db.QueryRowContext(ctx, `SELECT id FROM sources WHERE canonical_url=?`,
		fmt.Sprintf("regional://%s", bodyCode)).Scan(&srcID); err != nil {
		t.Fatal(err)
	}

	res, err = db.ExecContext(ctx, `
		INSERT INTO crawl_jobs (source_id, country_id, job_type, status)
		VALUES (?, ?, 'pdf_discovery', 'running')`, srcID, countryID)
	if err != nil {
		t.Fatal(err)
	}
	jobID, _ := res.LastInsertId()

	// Insert row with original_url set, report_url NULL.
	res, err = db.ExecContext(ctx, `
		INSERT INTO staged_regional_documents
			(crawl_job_id, country_id, body_code, ref, title, original_url)
		VALUES (?, ?, ?, ?, ?, ?)`,
		jobID, countryID, bodyCode,
		fmt.Sprintf("ref-%s-html-001", bodyCode),
		"HTML Report 001",
		originalURL)
	if err != nil {
		t.Fatal(err)
	}
	docID, _ = res.LastInsertId()
	return docID, countryID
}

// TestRegionalPendingDocsReturnsHtmlDoc verifies that a staged doc with
// original_url set and report_url NULL is returned by PendingDocs (IAC pattern).
func TestRegionalPendingDocsReturnsHtmlDoc(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	originalURL := "https://mak-iac.example/rassledovaniya/an-2-ra-40440-19-05-2026/"
	docID, _ := seedRegionalHtmlDoc(t, db, "RU", "IAC", originalURL)

	src := RegionalSource{HTTP: http.DefaultClient}
	docs, err := src.PendingDocs(ctx, db, 0)
	if err != nil {
		t.Fatalf("PendingDocs: %v", err)
	}
	var found bool
	for _, d := range docs {
		if d.ID == docID {
			found = true
			// ArchivedURL (report_url) must be empty for html-page docs.
			if d.ArchivedURL != "" {
				t.Fatalf("ArchivedURL=%q want empty for html-page doc", d.ArchivedURL)
			}
			// OriginalURL must be set.
			if d.OriginalURL != originalURL {
				t.Fatalf("OriginalURL=%q want %q", d.OriginalURL, originalURL)
			}
			if d.SourceRef != "IAC" {
				t.Fatalf("SourceRef=%q want IAC", d.SourceRef)
			}
			break
		}
	}
	if !found {
		t.Fatalf("html-page doc %d not returned by PendingDocs", docID)
	}
}

// TestRegionalEnsureDownloadedHtmlPage verifies that EnsureDownloaded handles
// the IAC html-page path: fetches original_url, strips HTML to text, writes
// a .txt file, sets ocr_text_path in the DB, sets doc.OCRTextPath.Valid=true,
// and sets download_status='downloaded'.
func TestRegionalEnsureDownloadedHtmlPage(t *testing.T) {
	allowLoopback(t)

	ctx := context.Background()
	db := newExtractTestDB(t)

	// IAC-style HTML report page with structured accident fields.
	const iacHTML = `<html><head>
		<title>Расследование — Ан-2 RA-40440</title>
		<style>.nav { display: none; }</style>
		<script>analytics();</script>
	</head><body>
		<h1>Расследование авиационного происшествия</h1>
		<table>
			<tr><td>Дата:</td><td>19.05.2026</td></tr>
			<tr><td>Воздушное судно:</td><td>Ан-2</td></tr>
			<tr><td>Регистрация:</td><td>RA-40440</td></tr>
			<tr><td>Оператор:</td><td>ООО &quot;Авиапром&quot;</td></tr>
			<tr><td>Погибших:</td><td>0</td></tr>
		</table>
		<p>Самолёт выполнял авиационные работы.</p>
	</body></html>`

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, iacHTML)
	}))
	defer srv.Close()

	docID, _ := seedRegionalHtmlDoc(t, db, "RU", "IAC", srv.URL+"/rassledovaniya/an-2-ra-40440/")
	src := RegionalSource{HTTP: srv.Client()}

	docs, err := src.PendingDocs(ctx, db, 0)
	if err != nil {
		t.Fatalf("PendingDocs: %v", err)
	}
	var doc ExtractDoc
	for _, d := range docs {
		if d.ID == docID {
			doc = d
			break
		}
	}
	if doc.ID == 0 {
		t.Fatalf("html-page doc %d not found in PendingDocs", docID)
	}

	storeDir := t.TempDir()
	if err := src.EnsureDownloaded(ctx, db, storeDir, &doc); err != nil {
		t.Fatalf("EnsureDownloaded (html): %v", err)
	}

	// doc.OCRTextPath must be set (signals core to skip OCR).
	if !doc.OCRTextPath.Valid || doc.OCRTextPath.String == "" {
		t.Fatal("doc.OCRTextPath must be set after EnsureDownloaded on html-page doc")
	}

	// The .txt file must exist and contain the visible field text.
	textBytes, err := os.ReadFile(doc.OCRTextPath.String)
	if err != nil {
		t.Fatalf("read text file %q: %v", doc.OCRTextPath.String, err)
	}
	textContent := string(textBytes)
	for _, want := range []string{"RA-40440", "19.05.2026", "Ан-2", "Самолёт"} {
		if !strings.Contains(textContent, want) {
			t.Errorf("text file missing %q\nfull content: %q", want, textContent)
		}
	}
	// Script/style content must be stripped.
	for _, mustNot := range []string{"analytics", "display: none"} {
		if strings.Contains(textContent, mustNot) {
			t.Errorf("text file must not contain %q\nfull content: %q", mustNot, textContent)
		}
	}

	// DB row must have download_status='downloaded' and ocr_text_path set.
	var dbStatus, dbOCRPath string
	if err := db.QueryRowContext(ctx,
		`SELECT download_status, coalesce(ocr_text_path,'') FROM staged_regional_documents WHERE id=?`, docID).
		Scan(&dbStatus, &dbOCRPath); err != nil {
		t.Fatalf("scan row: %v", err)
	}
	if dbStatus != "downloaded" {
		t.Fatalf("download_status=%q want downloaded", dbStatus)
	}
	if dbOCRPath == "" {
		t.Fatal("ocr_text_path must be set in DB for html-page doc")
	}
	if dbOCRPath != doc.OCRTextPath.String {
		t.Fatalf("DB ocr_text_path=%q, doc.OCRTextPath=%q — must match", dbOCRPath, doc.OCRTextPath.String)
	}

	// Path must be absolute.
	if !filepath.IsAbs(doc.OCRTextPath.String) {
		t.Fatalf("OCRTextPath must be absolute, got %q", doc.OCRTextPath.String)
	}
}

// TestRegionalEnsureDownloadedHtmlPageFailsOnEmptyBody verifies that an
// html-page that strips to empty text is treated as a download failure.
func TestRegionalEnsureDownloadedHtmlPageFailsOnEmptyBody(t *testing.T) {
	allowLoopback(t)

	ctx := context.Background()
	db := newExtractTestDB(t)

	// Serve a page with no visible text.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html")
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, `<html><head><style>body{}</style></head><body>   </body></html>`)
	}))
	defer srv.Close()

	docID, _ := seedRegionalHtmlDoc(t, db, "RU", "IAC", srv.URL+"/empty-page/")
	src := RegionalSource{HTTP: srv.Client()}

	docs, _ := src.PendingDocs(ctx, db, 0)
	var doc ExtractDoc
	for _, d := range docs {
		if d.ID == docID {
			doc = d
			break
		}
	}
	if doc.ID == 0 {
		t.Fatalf("html-page doc %d not found in PendingDocs", docID)
	}

	err := src.EnsureDownloaded(ctx, db, t.TempDir(), &doc)
	if err == nil {
		t.Fatal("expected error for empty-text html page, got nil")
	}

	var dbStatus string
	db.QueryRowContext(ctx, `SELECT download_status FROM staged_regional_documents WHERE id=?`, docID).Scan(&dbStatus)
	if dbStatus != "failed" {
		t.Fatalf("download_status=%q want failed for empty-text page", dbStatus)
	}
}

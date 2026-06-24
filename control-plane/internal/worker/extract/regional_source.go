package extract

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/atomicfile"
)

// RegionalSource is the StagedDocSource adapter for staged_regional_documents.
// Documents have a report_url pointing to a downloadable PDF; EnsureDownloaded
// fetches it and records the local path. It credits the regional investigation
// body (ECCAA/BAGAIA/IAC) identified by body_code as the source.
type RegionalSource struct {
	HTTP *http.Client
}

var _ StagedDocSource = RegionalSource{}

// Name identifies this source for logging.
func (RegionalSource) Name() string { return "regional" }

// PendingDocs returns staged_regional_documents rows that have either a
// report_url (PDF download path) or an original_url (html report page, e.g.
// IAC/МАК), have not yet been fully extracted, and have fewer than 3
// extraction attempts. Results are ordered by country priority descending,
// then document id ascending.
func (RegionalSource) PendingDocs(ctx context.Context, db *sql.DB, limit int) ([]ExtractDoc, error) {
	q := `
		SELECT d.id, d.country_id, c.iso2,
		       coalesce(d.digest,''), coalesce(d.local_file_path,''),
		       d.original_url, coalesce(d.report_url,''),
		       d.ocr_text_path, d.digest,
		       d.extraction_attempts, d.crawl_job_id, c.priority_score,
		       d.body_code
		  FROM staged_regional_documents d
		  JOIN countries c ON c.id = d.country_id
		 WHERE ((d.report_url IS NOT NULL AND d.report_url != '')
		        OR (d.original_url IS NOT NULL AND d.original_url != ''))
		   AND (
		     (d.download_status IN ('pending','failed') AND d.extraction_status IN ('pending','failed'))
		     OR
		     (d.download_status = 'downloaded' AND d.extraction_status IN ('pending','ocr_done','failed'))
		   )
		   AND d.extraction_attempts < 3
		 ORDER BY c.priority_score DESC, d.id ASC`
	if limit > 0 {
		q += fmt.Sprintf(" LIMIT %d", limit)
	}
	rows, err := db.QueryContext(ctx, q)
	if err != nil {
		return nil, fmt.Errorf("regional: select pending extract docs: %w", err)
	}
	defer rows.Close()

	var docs []ExtractDoc
	for rows.Next() {
		var d ExtractDoc
		var digestNull sql.NullString
		if err := rows.Scan(
			&d.ID, &d.CountryID, &d.ISO2,
			&d.Digest, &d.LocalFilePath,
			&d.OriginalURL, &d.ArchivedURL,
			&d.OCRTextPath, &digestNull,
			&d.Attempts, &d.CrawlJobID, &d.Priority,
			&d.SourceRef,
		); err != nil {
			return nil, fmt.Errorf("regional: scan extract doc: %w", err)
		}
		// digest may be NULL before download; keep the string form consistent.
		if digestNull.Valid {
			d.Digest = digestNull.String
		}
		docs = append(docs, d)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return docs, nil
}

// EnsureDownloaded prepares the document text for LLM extraction.
//
// For PDF docs (doc.ArchivedURL / report_url is non-empty): downloads the PDF,
// writes it to <storeDir>/<iso2>/<sha256hex>.pdf, and updates
// download_status/local_file_path/digest. The core then OCRs the PDF to text.
//
// For html-page docs (doc.ArchivedURL is empty, doc.OriginalURL is set, e.g.
// IAC/МАК pages): fetches the page via the SSRF-guarded fetchGuarded helper,
// strips HTML to plain text, writes a .txt file, and sets ocr_text_path so the
// extract core skips OCR and reads the text file directly.
//
// Idempotent: skips re-fetch when a local file already exists on disk.
// On error the row is set to download_status='failed'.
func (s RegionalSource) EnsureDownloaded(ctx context.Context, db *sql.DB, storeDir string, doc *ExtractDoc) error {
	// Idempotency: skip re-download if already downloaded and file is on disk.
	if doc.LocalFilePath != "" {
		if _, err := os.Stat(doc.LocalFilePath); err == nil {
			return nil
		}
	}

	if doc.ArchivedURL != "" {
		// ── PDF path (existing behaviour, unchanged) ──────────────────────────────
		localPath, digest, err := DownloadReportURL(ctx, s.HTTP, doc.ArchivedURL, storeDir, doc.ISO2)
		if err != nil {
			if _, ue := db.ExecContext(ctx, `
				UPDATE staged_regional_documents SET download_status='failed' WHERE id=?`, doc.ID); ue != nil {
				return fmt.Errorf("regional: mark download failed %d: %w", doc.ID, ue)
			}
			return fmt.Errorf("regional: download %s: %w", doc.ArchivedURL, err)
		}
		if _, err := db.ExecContext(ctx, `
			UPDATE staged_regional_documents
			   SET download_status='downloaded', local_file_path=?, digest=?
			 WHERE id=?`, localPath, digest, doc.ID); err != nil {
			return fmt.Errorf("regional: update after download %d: %w", doc.ID, err)
		}
		doc.LocalFilePath = localPath
		doc.Digest = digest
		return nil
	}

	// ── Html-page path (IAC/МАК and similar) ─────────────────────────────────
	body, err := fetchGuarded(ctx, s.HTTP, doc.OriginalURL)
	if err != nil {
		if _, ue := db.ExecContext(ctx, `
			UPDATE staged_regional_documents SET download_status='failed' WHERE id=?`, doc.ID); ue != nil {
			return fmt.Errorf("regional: mark html download failed %d: %w", doc.ID, ue)
		}
		return fmt.Errorf("regional: fetch html %s: %w", doc.OriginalURL, err)
	}

	text := htmlToText(body)
	if strings.TrimSpace(text) == "" {
		if _, ue := db.ExecContext(ctx, `
			UPDATE staged_regional_documents SET download_status='failed' WHERE id=?`, doc.ID); ue != nil {
			return fmt.Errorf("regional: mark html empty failed %d: %w", doc.ID, ue)
		}
		return fmt.Errorf("regional: html page %s stripped to empty text", doc.OriginalURL)
	}

	// Use sha256 of the raw page bytes for a content-addressed, stable filename.
	sum := sha256.Sum256(body)
	hexDigest := hex.EncodeToString(sum[:])

	dir := filepath.Join(storeDir, doc.ISO2)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("regional: mkdir %s: %w", dir, err)
	}
	textPath := filepath.Join(dir, hexDigest+".txt")
	if err := atomicfile.Write(textPath, []byte(text)); err != nil {
		return fmt.Errorf("regional: write html text %s: %w", textPath, err)
	}

	if _, err := db.ExecContext(ctx, `
		UPDATE staged_regional_documents
		   SET download_status='downloaded', ocr_text_path=?, local_file_path=?
		 WHERE id=?`, textPath, textPath, doc.ID); err != nil {
		return fmt.Errorf("regional: update after html fetch %d: %w", doc.ID, err)
	}
	doc.OCRTextPath = sql.NullString{String: textPath, Valid: true}
	doc.LocalFilePath = textPath
	doc.Digest = hexDigest
	return nil
}

// ResolveSource credits the regional body identified by doc.SourceRef (body_code).
// It looks up the body in regional_bodies by code, then upserts a source row with
// source_type='regional_body'. Returns tier=4 and copyright="official_public".
// Tier 4 is the only tier model.SourceTierAllowsType permits for
// source_type='regional_body', so the row passes the Invariant-9 validator.
func (RegionalSource) ResolveSource(ctx context.Context, q execQuerier, doc ExtractDoc) (int64, int, string, error) {
	var name, websiteURL, sourceURL string
	err := q.QueryRowContext(ctx, `
		SELECT name, coalesce(website_url,''), source_url
		  FROM regional_bodies WHERE code = ?`, doc.SourceRef).
		Scan(&name, &websiteURL, &sourceURL)
	if err == sql.ErrNoRows {
		return 0, 0, "", fmt.Errorf("regional: unknown body code %q", doc.SourceRef)
	}
	if err != nil {
		return 0, 0, "", fmt.Errorf("regional: lookup body %q: %w", doc.SourceRef, err)
	}

	url := websiteURL
	if url == "" {
		url = sourceURL
	}
	canonical := "regional://" + doc.SourceRef
	id, e := upsertSource(ctx, q, name, url, canonical, "regional_body", 4)
	if e != nil {
		return 0, 0, "", e
	}
	return id, 4, "official_public", nil
}

// MarkSkipped advances the document to extraction_status='skipped'.
func (RegionalSource) MarkSkipped(ctx context.Context, db *sql.DB, id int64) error {
	if _, err := db.ExecContext(ctx,
		`UPDATE staged_regional_documents SET extraction_status='skipped' WHERE id=?`, id); err != nil {
		return fmt.Errorf("regional: mark skipped %d: %w", id, err)
	}
	return nil
}

// MarkExtractedTx links the document to its event and advances it to 'extracted'
// inside the caller's transaction, so it commits atomically with the promotion.
func (RegionalSource) MarkExtractedTx(ctx context.Context, tx *sql.Tx, id, eventID int64) error {
	if _, err := tx.ExecContext(ctx, `
		UPDATE staged_regional_documents SET event_id=?, extraction_status='extracted' WHERE id=?`,
		eventID, id); err != nil {
		return fmt.Errorf("regional: mark extracted %d: %w", id, err)
	}
	return nil
}

// RecordFailure marks the row failed, bumps the attempt counter, and logs a
// crawl_errors row with the classified errType.
func (RegionalSource) RecordFailure(ctx context.Context, db *sql.DB, doc ExtractDoc, url, errType string, cause error) error {
	if _, err := db.ExecContext(ctx, `
		UPDATE staged_regional_documents
		   SET extraction_status='failed', extraction_error=?, extraction_attempts=extraction_attempts+1
		 WHERE id=?`, cause.Error(), doc.ID); err != nil {
		return fmt.Errorf("regional: mark failed %d: %w", doc.ID, err)
	}
	_, _ = db.ExecContext(ctx, `
		INSERT INTO crawl_errors (crawl_job_id, url, error_type, message)
		SELECT crawl_job_id, ?, ?, ? FROM staged_regional_documents WHERE id=?`,
		url, errType, cause.Error(), doc.ID)
	return nil
}

// PersistOCRPath records the OCR text path and advances the row to 'ocr_done'.
func (RegionalSource) PersistOCRPath(ctx context.Context, db *sql.DB, id int64, path string) error {
	if _, err := db.ExecContext(ctx, `
		UPDATE staged_regional_documents
		   SET ocr_text_path = ?, extraction_status = 'ocr_done'
		 WHERE id = ?`, path, id); err != nil {
		return fmt.Errorf("regional: mark ocr_done %d: %w", id, err)
	}
	return nil
}

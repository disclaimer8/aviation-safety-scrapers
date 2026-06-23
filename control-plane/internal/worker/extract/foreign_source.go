package extract

import (
	"context"
	"database/sql"
	"fmt"
	"net/http"
)

// ForeignSource is the StagedDocSource adapter for staged_foreign_documents.
// Documents have a report_url pointing to a downloadable PDF; EnsureDownloaded
// fetches it and records the local path. It credits the foreign accredited
// authority (NTSB/BEA/ATSB) identified by the authority column as the source.
type ForeignSource struct {
	HTTP *http.Client
}

var _ StagedDocSource = ForeignSource{}

// foreignAuthorityMeta holds the display name and website URL for each
// supported foreign authority. Keys are the authority column values as stored
// in staged_foreign_documents (lower-case).
var foreignAuthorityMeta = map[string]struct {
	name string
	url  string
}{
	"ntsb": {"National Transportation Safety Board", "https://www.ntsb.gov"},
	"bea":  {"Bureau d'Enquêtes et d'Analyses", "https://www.bea.aero"},
	"atsb": {"Australian Transport Safety Bureau", "https://www.atsb.gov.au"},
}

// Name identifies this source for logging.
func (ForeignSource) Name() string { return "foreign" }

// PendingDocs returns staged_foreign_documents rows that have a report_url
// (page-only docs without a downloadable report are MVP-deferred), have not
// yet been fully extracted, and have fewer than 3 extraction attempts.
// Results are ordered by country priority descending, then document id ascending.
func (ForeignSource) PendingDocs(ctx context.Context, db *sql.DB, limit int) ([]ExtractDoc, error) {
	q := `
		SELECT d.id, d.country_id, c.iso2,
		       coalesce(d.digest,''), coalesce(d.local_file_path,''),
		       d.original_url, d.report_url,
		       d.ocr_text_path, d.digest,
		       d.extraction_attempts, d.crawl_job_id, c.priority_score,
		       d.authority
		  FROM staged_foreign_documents d
		  JOIN countries c ON c.id = d.country_id
		 WHERE d.report_url IS NOT NULL AND d.report_url != ''
		   AND (
		     (d.download_status IN ('pending','failed') AND d.extraction_status = 'pending')
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
		return nil, fmt.Errorf("foreign: select pending extract docs: %w", err)
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
			return nil, fmt.Errorf("foreign: scan extract doc: %w", err)
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

// EnsureDownloaded downloads doc.ArchivedURL (the report_url), writes it to
// <storeDir>/<iso2>/<sha256hex>.pdf, and updates download_status/local_file_path/digest
// on the staged row. On error the row is set to download_status='failed'.
func (s ForeignSource) EnsureDownloaded(ctx context.Context, db *sql.DB, storeDir string, doc *ExtractDoc) error {
	localPath, digest, err := DownloadReportURL(ctx, s.HTTP, doc.ArchivedURL, storeDir, doc.ISO2)
	if err != nil {
		if _, ue := db.ExecContext(ctx, `
			UPDATE staged_foreign_documents SET download_status='failed' WHERE id=?`, doc.ID); ue != nil {
			return fmt.Errorf("foreign: mark download failed %d: %w", doc.ID, ue)
		}
		return fmt.Errorf("foreign: download %s: %w", doc.ArchivedURL, err)
	}
	if _, err := db.ExecContext(ctx, `
		UPDATE staged_foreign_documents
		   SET download_status='downloaded', local_file_path=?, digest=?
		 WHERE id=?`, localPath, digest, doc.ID); err != nil {
		return fmt.Errorf("foreign: update after download %d: %w", doc.ID, err)
	}
	doc.LocalFilePath = localPath
	doc.Digest = digest
	return nil
}

// ResolveSource credits the foreign authority identified by doc.SourceRef (authority code).
// It looks up the authority in the static foreignAuthorityMeta map, then upserts a source
// row with source_type='foreign_authority'. Returns tier=2 and copyright="official_public".
func (ForeignSource) ResolveSource(ctx context.Context, q execQuerier, doc ExtractDoc) (int64, int, string, error) {
	meta, ok := foreignAuthorityMeta[doc.SourceRef]
	if !ok {
		return 0, 0, "", fmt.Errorf("foreign: unknown authority %q", doc.SourceRef)
	}

	canonical := "foreign://" + doc.SourceRef
	id, e := upsertSource(ctx, q, meta.name, meta.url, canonical, "official_foreign_accredited_rep", 2)
	if e != nil {
		return 0, 0, "", e
	}
	return id, 2, "official_public", nil
}

// MarkSkipped advances the document to extraction_status='skipped'.
func (ForeignSource) MarkSkipped(ctx context.Context, db *sql.DB, id int64) error {
	if _, err := db.ExecContext(ctx,
		`UPDATE staged_foreign_documents SET extraction_status='skipped' WHERE id=?`, id); err != nil {
		return fmt.Errorf("foreign: mark skipped %d: %w", id, err)
	}
	return nil
}

// MarkExtractedTx links the document to its event and advances it to 'extracted'
// inside the caller's transaction, so it commits atomically with the promotion.
func (ForeignSource) MarkExtractedTx(ctx context.Context, tx *sql.Tx, id, eventID int64) error {
	if _, err := tx.ExecContext(ctx, `
		UPDATE staged_foreign_documents SET event_id=?, extraction_status='extracted' WHERE id=?`,
		eventID, id); err != nil {
		return fmt.Errorf("foreign: mark extracted %d: %w", id, err)
	}
	return nil
}

// RecordFailure marks the row failed, bumps the attempt counter, and logs a
// crawl_errors row with the classified errType.
func (ForeignSource) RecordFailure(ctx context.Context, db *sql.DB, doc ExtractDoc, url, errType string, cause error) error {
	if _, err := db.ExecContext(ctx, `
		UPDATE staged_foreign_documents
		   SET extraction_status='failed', extraction_error=?, extraction_attempts=extraction_attempts+1
		 WHERE id=?`, cause.Error(), doc.ID); err != nil {
		return fmt.Errorf("foreign: mark failed %d: %w", doc.ID, err)
	}
	_, _ = db.ExecContext(ctx, `
		INSERT INTO crawl_errors (crawl_job_id, url, error_type, message)
		SELECT crawl_job_id, ?, ?, ? FROM staged_foreign_documents WHERE id=?`,
		url, errType, cause.Error(), doc.ID)
	return nil
}

// PersistOCRPath records the OCR text path and advances the row to 'ocr_done'.
func (ForeignSource) PersistOCRPath(ctx context.Context, db *sql.DB, id int64, path string) error {
	if _, err := db.ExecContext(ctx, `
		UPDATE staged_foreign_documents
		   SET ocr_text_path = ?, extraction_status = 'ocr_done'
		 WHERE id = ?`, path, id); err != nil {
		return fmt.Errorf("foreign: mark ocr_done %d: %w", id, err)
	}
	return nil
}

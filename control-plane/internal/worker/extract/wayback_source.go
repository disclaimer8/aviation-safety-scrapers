package extract

import (
	"context"
	"database/sql"
	"fmt"
)

// WaybackSource is the StagedDocSource adapter for staged_wayback_documents.
// Documents are pre-downloaded by the wayback download stage, so EnsureDownloaded
// is a no-op. It credits the country's national_aai/caa authority as a tier-1
// official source, falling back to a per-country tier-2 wayback source.
type WaybackSource struct{}

var _ StagedDocSource = WaybackSource{}

// Name identifies this source for logging.
func (WaybackSource) Name() string { return "wayback" }

// PendingDocs runs the existing wayback extract SELECT: downloaded documents with
// a non-terminal extraction status and fewer than 3 attempts, highest country
// priority first.
func (WaybackSource) PendingDocs(ctx context.Context, db *sql.DB, limit int) ([]ExtractDoc, error) {
	q := `
		SELECT d.id, d.country_id, c.iso2, d.digest, d.local_file_path, d.original_url,
		       d.archived_url, d.ocr_text_path, d.checksum, coalesce(c.wayback_target,''),
		       d.extraction_attempts, d.crawl_job_id, c.priority_score
		  FROM staged_wayback_documents d
		  JOIN countries c ON c.id = d.country_id
		 WHERE d.download_status = 'downloaded'
		   AND d.extraction_status IN ('pending','ocr_done','failed')
		   AND d.extraction_attempts < 3
		 ORDER BY c.priority_score DESC, d.id ASC`
	if limit > 0 {
		q += fmt.Sprintf(" LIMIT %d", limit)
	}
	rows, err := db.QueryContext(ctx, q)
	if err != nil {
		return nil, fmt.Errorf("wayback: select pending extract docs: %w", err)
	}
	defer rows.Close()
	var docs []ExtractDoc
	for rows.Next() {
		var d ExtractDoc
		if err := rows.Scan(&d.ID, &d.CountryID, &d.ISO2, &d.Digest, &d.LocalFilePath, &d.OriginalURL,
			&d.ArchivedURL, &d.OCRTextPath, &d.Checksum, &d.WaybackTarget, &d.Attempts,
			&d.CrawlJobID, &d.Priority); err != nil {
			return nil, fmt.Errorf("wayback: scan extract doc: %w", err)
		}
		docs = append(docs, d)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return docs, nil
}

// EnsureDownloaded is a no-op: wayback documents are already on disk.
func (WaybackSource) EnsureDownloaded(ctx context.Context, db *sql.DB, storeDir string, doc *ExtractDoc) error {
	return nil
}

// ResolveSource prefers the country's national_aai authority (else caa) as an
// official_aai tier-1 source; failing that it falls back to a per-country wayback
// tier-5 source built from the country's wayback_target. Tier 5 is the only tier
// model.SourceTierAllowsType permits for source_type='wayback', so the fallback
// row passes the Invariant-9 validator. Lookup-or-create keys on
// UNIQUE(canonical_url, source_type).
func (WaybackSource) ResolveSource(ctx context.Context, q execQuerier, doc ExtractDoc) (int64, int, string, error) {
	var name, website, archive sql.NullString
	err := q.QueryRowContext(ctx, `
		SELECT name, website_url, archive_url FROM authorities
		 WHERE country_id = ? AND type IN ('national_aai','caa')
		 ORDER BY CASE type WHEN 'national_aai' THEN 0 ELSE 1 END, id ASC
		 LIMIT 1`, doc.CountryID).Scan(&name, &website, &archive)
	if err != nil && err != sql.ErrNoRows {
		return 0, 0, "", fmt.Errorf("wayback: lookup authority %d: %w", doc.CountryID, err)
	}

	if err == nil && name.Valid {
		canonical := archive.String
		if canonical == "" {
			canonical = website.String
		}
		if canonical != "" {
			id, e := upsertSource(ctx, q, name.String, website.String, canonical, "official_aai", 1)
			if e != nil {
				return 0, 0, "", e
			}
			return id, 1, "official_public", nil
		}
	}

	// Fallback: wayback source from the target domain.
	canonical := "wayback://" + doc.WaybackTarget
	id, e := upsertSource(ctx, q, "Internet Archive: "+doc.WaybackTarget, "https://"+doc.WaybackTarget, canonical, "wayback", 5)
	if e != nil {
		return 0, 0, "", e
	}
	return id, 5, "unknown", nil
}

// MarkSkipped advances the document to extraction_status='skipped'.
func (WaybackSource) MarkSkipped(ctx context.Context, db *sql.DB, id int64) error {
	if _, err := db.ExecContext(ctx,
		`UPDATE staged_wayback_documents SET extraction_status='skipped' WHERE id=?`, id); err != nil {
		return fmt.Errorf("wayback: mark skipped %d: %w", id, err)
	}
	return nil
}

// MarkExtractedTx links the document to its event and advances it to 'extracted'
// inside the caller's transaction, so it commits atomically with the promotion.
func (WaybackSource) MarkExtractedTx(ctx context.Context, tx *sql.Tx, id, eventID int64) error {
	if _, err := tx.ExecContext(ctx, `
		UPDATE staged_wayback_documents SET event_id=?, extraction_status='extracted' WHERE id=?`,
		eventID, id); err != nil {
		return fmt.Errorf("wayback: mark extracted %d: %w", id, err)
	}
	return nil
}

// RecordFailure marks the row failed, bumps the attempt counter, and logs a
// crawl_errors row against the document's crawl_job with the classified errType.
func (WaybackSource) RecordFailure(ctx context.Context, db *sql.DB, doc ExtractDoc, url, errType string, cause error) error {
	if _, err := db.ExecContext(ctx, `
		UPDATE staged_wayback_documents
		   SET extraction_status='failed', extraction_error=?, extraction_attempts=extraction_attempts+1
		 WHERE id=?`, cause.Error(), doc.ID); err != nil {
		return fmt.Errorf("wayback: mark failed %d: %w", doc.ID, err)
	}
	_, _ = db.ExecContext(ctx, `
		INSERT INTO crawl_errors (crawl_job_id, url, error_type, message)
		SELECT crawl_job_id, ?, ?, ? FROM staged_wayback_documents WHERE id=?`,
		url, errType, cause.Error(), doc.ID)
	return nil
}

// PersistOCRPath records the OCR text path and advances the row to 'ocr_done'.
func (WaybackSource) PersistOCRPath(ctx context.Context, db *sql.DB, id int64, path string) error {
	if _, err := db.ExecContext(ctx, `
		UPDATE staged_wayback_documents
		   SET ocr_text_path = ?, extraction_status = 'ocr_done'
		 WHERE id = ?`, path, id); err != nil {
		return fmt.Errorf("wayback: mark ocr_done %d: %w", id, err)
	}
	return nil
}

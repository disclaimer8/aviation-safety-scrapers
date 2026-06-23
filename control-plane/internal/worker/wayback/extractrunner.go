package wayback

import (
	"context"
	"database/sql"
	"fmt"
	"os"
)

// ExtractStats is the aggregate result of a batch run.
type ExtractStats struct {
	OCRDone   int
	Extracted int
	Skipped   int
	Failed    int
}

// ExtractOne runs one document through the state machine: OCR (when no text
// artifact yet) then extract+promote. Data-level failures are recorded on the
// row (status='failed', attempt++, crawl_errors) and returned as status without
// an error; only unexpected DB failures return an error.
func ExtractOne(ctx context.Context, db *sql.DB, ocr OCRClient, llm LLMClient, storeDir string, doc ExtractDoc) (string, error) {
	// OCR step.
	if !doc.OCRTextPath.Valid || doc.OCRTextPath.String == "" {
		pdf, err := os.ReadFile(doc.LocalFilePath)
		if err != nil {
			return recordExtractFailure(ctx, db, doc, doc.LocalFilePath, err)
		}
		text, err := ocr.OCR(ctx, pdf)
		if err != nil {
			return recordExtractFailure(ctx, db, doc, doc.ArchivedURL, err)
		}
		path, err := PersistOCRText(ctx, db, storeDir, doc.ISO2, doc.Digest, doc.ID, text)
		if err != nil {
			return "", err
		}
		doc.OCRTextPath = sql.NullString{String: path, Valid: true}
	}

	// Extract step.
	text, err := os.ReadFile(doc.OCRTextPath.String)
	if err != nil {
		return recordExtractFailure(ctx, db, doc, doc.OCRTextPath.String, err)
	}
	raw, err := llm.Extract(ctx, string(text))
	if err != nil {
		return recordExtractFailure(ctx, db, doc, doc.ArchivedURL, err)
	}
	e := NormalizeEvent(raw)
	if !raw.IsAviationAccident || !HasCriticalFields(e) {
		if _, err := db.ExecContext(ctx,
			`UPDATE staged_wayback_documents SET extraction_status='skipped' WHERE id=?`, doc.ID); err != nil {
			return "", fmt.Errorf("wayback: mark skipped %d: %w", doc.ID, err)
		}
		return "skipped", nil
	}
	if _, _, err := PromoteDocument(ctx, db, doc, e); err != nil {
		return "", err
	}
	return "extracted", nil
}

// recordExtractFailure marks the row failed, bumps the attempt counter, and logs
// a crawl_errors row against the document's crawl_job. Returns status "failed".
func recordExtractFailure(ctx context.Context, db *sql.DB, doc ExtractDoc, url string, cause error) (string, error) {
	if _, err := db.ExecContext(ctx, `
		UPDATE staged_wayback_documents
		   SET extraction_status='failed', extraction_error=?, extraction_attempts=extraction_attempts+1
		 WHERE id=?`, cause.Error(), doc.ID); err != nil {
		return "", fmt.Errorf("wayback: mark failed %d: %w", doc.ID, err)
	}
	_, _ = db.ExecContext(ctx, `
		INSERT INTO crawl_errors (crawl_job_id, url, error_type, message)
		SELECT crawl_job_id, ?, 'unknown', ? FROM staged_wayback_documents WHERE id=?`,
		url, cause.Error(), doc.ID)
	return "failed", nil
}

// ProcessExtractPending runs up to limit documents needing extraction, highest
// country priority first. limit <= 0 means no cap.
func ProcessExtractPending(ctx context.Context, db *sql.DB, ocr OCRClient, llm LLMClient, storeDir string, limit int) (ExtractStats, error) {
	q := `
		SELECT d.id, d.country_id, c.iso2, d.digest, d.local_file_path, d.original_url,
		       d.archived_url, d.ocr_text_path, d.checksum, coalesce(c.wayback_target,''),
		       d.extraction_attempts
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
		return ExtractStats{}, fmt.Errorf("wayback: select pending extract docs: %w", err)
	}
	var docs []ExtractDoc
	for rows.Next() {
		var d ExtractDoc
		if err := rows.Scan(&d.ID, &d.CountryID, &d.ISO2, &d.Digest, &d.LocalFilePath, &d.OriginalURL,
			&d.ArchivedURL, &d.OCRTextPath, &d.Checksum, &d.WaybackTarget, &d.Attempts); err != nil {
			rows.Close()
			return ExtractStats{}, fmt.Errorf("wayback: scan extract doc: %w", err)
		}
		docs = append(docs, d)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return ExtractStats{}, err
	}
	rows.Close()

	var stats ExtractStats
	for _, d := range docs {
		status, err := ExtractOne(ctx, db, ocr, llm, storeDir, d)
		if err != nil {
			return stats, err
		}
		switch status {
		case "extracted":
			stats.Extracted++
		case "skipped":
			stats.Skipped++
		case "failed":
			stats.Failed++
		case "ocr_done":
			stats.OCRDone++
		}
	}
	return stats, nil
}

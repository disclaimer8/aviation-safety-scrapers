package extract

import (
	"context"
	"database/sql"
	"fmt"
	"net/http"
	"os"
	"strings"
)

// manufacturerTier is the source_tier credited to manufacturer safety
// publications (e.g. Airbus Safety First). They are authoritative but secondary
// — not official accident-investigation authorities — so they sit below the
// tier-2 government bodies (regional/foreign) and never count as "official"
// (promote.go treats only tier==1 as official).
const manufacturerTier = 3

// manufacturerPriority orders manufacturer docs within the cross-source extract
// queue. Manufacturer documents carry no country and therefore no
// countries.priority_score; the corpus is small and finite (one publication's
// back-issues), so a single high constant lets it drain promptly in a few passes
// rather than being perpetually starved behind the unbounded country backlog.
const manufacturerPriority = 100.0

// ManufacturerSource is the StagedDocSource adapter for
// staged_manufacturer_documents (Airbus Safety First and future manufacturer
// publications). Unlike the country-driven sources, these documents are GLOBAL:
// they have no country_id, no crawl_job, and no wayback target. Consequences:
//   - PendingDocs leaves CountryID/CrawlJobID at zero and uses a fixed priority;
//     ISO2 carries a manufacturer slug only so files land in a per-manufacturer
//     store sub-directory.
//   - ResolveSource credits the manufacturer publication itself (source_type
//     'manufacturer'); there is no per-body lookup table.
//   - RecordFailure does NOT write a crawl_errors row (crawl_errors.crawl_job_id
//     is NOT NULL and these docs have no job); the cause is preserved in the
//     staged row's extraction_error column.
//   - Promotion writes events with a NULL occurrence_country_id (see
//     promote.go's nullInt64 on doc.CountryID).
//
// Documents are PDFs: report_url points to the downloadable PDF and
// EnsureDownloaded fetches it; the core then OCRs and extracts it.
type ManufacturerSource struct {
	HTTP *http.Client
}

var _ StagedDocSource = ManufacturerSource{}

// Name identifies this source for logging.
func (ManufacturerSource) Name() string { return "manufacturer" }

// manufacturerSlug lowercases a manufacturer/publication name into a filesystem-
// and URL-safe segment (alnum runs joined by single hyphens). "Safety First" →
// "safety-first"; "Airbus" → "airbus".
func manufacturerSlug(s string) string {
	var b strings.Builder
	prevHyphen := false
	for _, r := range strings.ToLower(strings.TrimSpace(s)) {
		switch {
		case (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9'):
			b.WriteRune(r)
			prevHyphen = false
		default:
			if !prevHyphen && b.Len() > 0 {
				b.WriteByte('-')
				prevHyphen = true
			}
		}
	}
	return strings.TrimRight(b.String(), "-")
}

// PendingDocs returns staged_manufacturer_documents rows that have a report_url
// (PDF download target), have not yet been fully extracted, and have fewer than
// 3 extraction attempts. There is no country join; results are ordered by id.
func (ManufacturerSource) PendingDocs(ctx context.Context, db *sql.DB, limit int) ([]ExtractDoc, error) {
	q := `
		SELECT id, manufacturer, original_url, coalesce(report_url,''),
		       coalesce(local_file_path,''), ocr_text_path, digest,
		       extraction_attempts
		  FROM staged_manufacturer_documents
		 WHERE report_url IS NOT NULL AND report_url != ''
		   AND (
		     (download_status IN ('pending','failed') AND extraction_status IN ('pending','failed'))
		     OR
		     (download_status = 'downloaded' AND extraction_status IN ('pending','ocr_done','failed'))
		   )
		   AND extraction_attempts < 3
		 ORDER BY id ASC`
	if limit > 0 {
		q += fmt.Sprintf(" LIMIT %d", limit)
	}
	rows, err := db.QueryContext(ctx, q)
	if err != nil {
		return nil, fmt.Errorf("manufacturer: select pending extract docs: %w", err)
	}
	defer rows.Close()

	var docs []ExtractDoc
	for rows.Next() {
		var d ExtractDoc
		var digestNull sql.NullString
		if err := rows.Scan(
			&d.ID, &d.SourceRef, &d.OriginalURL, &d.ArchivedURL,
			&d.LocalFilePath, &d.OCRTextPath, &digestNull,
			&d.Attempts,
		); err != nil {
			return nil, fmt.Errorf("manufacturer: scan extract doc: %w", err)
		}
		if digestNull.Valid {
			d.Digest = digestNull.String
		}
		// Global doc: no country/job. ISO2 is a store-dir segment only.
		d.ISO2 = manufacturerSlug(d.SourceRef)
		d.Priority = manufacturerPriority
		docs = append(docs, d)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return docs, nil
}

// EnsureDownloaded fetches the report_url PDF, writes it to
// <storeDir>/<manufacturer-slug>/<sha256>.pdf, and records
// download_status/local_file_path/digest. Idempotent: skips re-download when a
// local file already exists. On error the row is set to download_status='failed'.
func (s ManufacturerSource) EnsureDownloaded(ctx context.Context, db *sql.DB, storeDir string, doc *ExtractDoc) error {
	if doc.LocalFilePath != "" {
		if _, err := os.Stat(doc.LocalFilePath); err == nil {
			return nil
		}
	}
	if doc.ArchivedURL == "" {
		if _, ue := db.ExecContext(ctx, `
			UPDATE staged_manufacturer_documents SET download_status='failed' WHERE id=?`, doc.ID); ue != nil {
			return fmt.Errorf("manufacturer: mark no-report failed %d: %w", doc.ID, ue)
		}
		return fmt.Errorf("manufacturer: doc %d has no report_url", doc.ID)
	}

	localPath, digest, err := DownloadReportURL(ctx, s.HTTP, doc.ArchivedURL, storeDir, doc.ISO2)
	if err != nil {
		if _, ue := db.ExecContext(ctx, `
			UPDATE staged_manufacturer_documents SET download_status='failed' WHERE id=?`, doc.ID); ue != nil {
			return fmt.Errorf("manufacturer: mark download failed %d: %w", doc.ID, ue)
		}
		return fmt.Errorf("manufacturer: download %s: %w", doc.ArchivedURL, err)
	}
	if _, err := db.ExecContext(ctx, `
		UPDATE staged_manufacturer_documents
		   SET download_status='downloaded', local_file_path=?, digest=?
		 WHERE id=?`, localPath, digest, doc.ID); err != nil {
		return fmt.Errorf("manufacturer: update after download %d: %w", doc.ID, err)
	}
	doc.LocalFilePath = localPath
	doc.Digest = digest
	return nil
}

// ResolveSource credits the manufacturer publication itself. It re-reads the
// manufacturer/publication/original_url from the staged row, upserts a source
// with source_type='manufacturer', and returns tier=manufacturerTier and
// copyright='metadata_only' (manufacturer safety publications are copyrighted and
// reprintable with attribution — fulltext is kept internally for extraction but
// not re-published; downstream surfaces only derived/original content).
func (ManufacturerSource) ResolveSource(ctx context.Context, q execQuerier, doc ExtractDoc) (int64, int, string, error) {
	var manufacturer, publication, originalURL string
	err := q.QueryRowContext(ctx, `
		SELECT manufacturer, publication, original_url
		  FROM staged_manufacturer_documents WHERE id = ?`, doc.ID).
		Scan(&manufacturer, &publication, &originalURL)
	if err == sql.ErrNoRows {
		return 0, 0, "", fmt.Errorf("manufacturer: staged doc %d not found", doc.ID)
	}
	if err != nil {
		return 0, 0, "", fmt.Errorf("manufacturer: lookup doc %d: %w", doc.ID, err)
	}

	name := strings.TrimSpace(manufacturer + " " + publication)
	canonical := "manufacturer://" + manufacturerSlug(manufacturer) + "/" + manufacturerSlug(publication)
	id, e := upsertSource(ctx, q, name, originalURL, canonical, "manufacturer", manufacturerTier)
	if e != nil {
		return 0, 0, "", e
	}
	return id, manufacturerTier, "metadata_only", nil
}

// MarkSkipped advances the document to extraction_status='skipped'.
func (ManufacturerSource) MarkSkipped(ctx context.Context, db *sql.DB, id int64) error {
	if _, err := db.ExecContext(ctx,
		`UPDATE staged_manufacturer_documents SET extraction_status='skipped' WHERE id=?`, id); err != nil {
		return fmt.Errorf("manufacturer: mark skipped %d: %w", id, err)
	}
	return nil
}

// MarkExtractedTx links the document to its event and advances it to 'extracted'
// inside the caller's transaction, committing atomically with the promotion.
func (ManufacturerSource) MarkExtractedTx(ctx context.Context, tx *sql.Tx, id, eventID int64) error {
	if _, err := tx.ExecContext(ctx, `
		UPDATE staged_manufacturer_documents SET event_id=?, extraction_status='extracted' WHERE id=?`,
		eventID, id); err != nil {
		return fmt.Errorf("manufacturer: mark extracted %d: %w", id, err)
	}
	return nil
}

// RecordFailure marks the row failed and bumps the attempt counter. Unlike the
// country-driven sources it does NOT log a crawl_errors row: manufacturer docs
// have no crawl_job and crawl_errors.crawl_job_id is NOT NULL. The cause is
// preserved in the staged row's extraction_error column. url/errType are accepted
// for interface parity but unused.
func (ManufacturerSource) RecordFailure(ctx context.Context, db *sql.DB, doc ExtractDoc, url, errType string, cause error) error {
	if _, err := db.ExecContext(ctx, `
		UPDATE staged_manufacturer_documents
		   SET extraction_status='failed', extraction_error=?, extraction_attempts=extraction_attempts+1
		 WHERE id=?`, cause.Error(), doc.ID); err != nil {
		return fmt.Errorf("manufacturer: mark failed %d: %w", doc.ID, err)
	}
	return nil
}

// PersistOCRPath records the OCR text path and advances the row to 'ocr_done'.
func (ManufacturerSource) PersistOCRPath(ctx context.Context, db *sql.DB, id int64, path string) error {
	if _, err := db.ExecContext(ctx, `
		UPDATE staged_manufacturer_documents
		   SET ocr_text_path = ?, extraction_status = 'ocr_done'
		 WHERE id = ?`, path, id); err != nil {
		return fmt.Errorf("manufacturer: mark ocr_done %d: %w", id, err)
	}
	return nil
}

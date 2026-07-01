package extract

import (
	"context"
	"database/sql"
	"os"
	"path/filepath"
	"sort"
)

// Error-type classifications recorded on crawl_errors for a failed extraction.
// Values MUST satisfy the crawl_errors.error_type CHECK constraint (see
// migrations/sql/002_pipeline.sql). No granular enum members exist for
// transport/OCR/LLM failures, so they map to 'unknown' — the detailed cause is
// preserved in the crawl_errors.message text. Promotion failures map to the real
// 'parse_error' member.
const (
	errTypeTransport = "unknown"     // file fetch / read failures
	errTypeOCR       = "unknown"     // OCR step failures
	errTypeLLM       = "unknown"     // LLM extraction failures
	errTypeParse     = "parse_error" // promotion / persistence failures
)

// extractOne runs one document through the state machine: ensure-downloaded, OCR
// (when no text artifact yet), then extract+promote. Data-level failures are
// recorded on the row (status='failed', attempt++, crawl_errors) and returned as
// status without an error; unexpected DB failures return an error. A THIRD case
// (GO-CP-3): a connection-level failure reaching the OCR or LLM endpoint itself
// (dial refused/timeout — the endpoint is down, not this document) returns an
// *InfraAbortError instead of recording a failure — see infra.go. The document's
// extraction_attempts is deliberately left untouched (there's nothing wrong with
// it) and the error propagates up through ProcessExtractPending, aborting the
// rest of the batch immediately rather than burning every remaining document's
// attempt budget against an outage that has nothing to do with them.
func extractOne(ctx context.Context, db *sql.DB, src StagedDocSource, ocr OCRClient, llm LLMClient, storeDir string, doc ExtractDoc) (string, error) {
	if err := src.EnsureDownloaded(ctx, db, storeDir, &doc); err != nil {
		return recordFailure(ctx, db, src, doc, doc.OriginalURL, errTypeTransport, err)
	}

	// OCR step.
	if !doc.OCRTextPath.Valid || doc.OCRTextPath.String == "" {
		pdf, err := os.ReadFile(doc.LocalFilePath)
		if err != nil {
			return recordFailure(ctx, db, src, doc, doc.LocalFilePath, errTypeTransport, err)
		}
		text, err := ocr.OCR(ctx, pdf)
		if err != nil {
			if isInfraError(err) {
				return "", &InfraAbortError{DocID: doc.ID, Step: "ocr", Cause: err}
			}
			return recordFailure(ctx, db, src, doc, doc.ArchivedURL, errTypeOCR, err)
		}
		path, err := PersistOCRText(ctx, db, src, storeDir, doc.ISO2, doc.Digest, doc.ID, text)
		if err != nil {
			return "", err
		}
		doc.OCRTextPath = sql.NullString{String: path, Valid: true}
	}

	// Extract step.
	text, err := os.ReadFile(doc.OCRTextPath.String)
	if err != nil {
		return recordFailure(ctx, db, src, doc, doc.OCRTextPath.String, errTypeTransport, err)
	}
	raw, err := llm.Extract(ctx, string(text))
	if err != nil {
		if isInfraError(err) {
			return "", &InfraAbortError{DocID: doc.ID, Step: "llm", Cause: err}
		}
		return recordFailure(ctx, db, src, doc, doc.ArchivedURL, errTypeLLM, err)
	}
	e := NormalizeEvent(raw)
	if !raw.IsAviationAccident || !HasCriticalFields(e) {
		if err := src.MarkSkipped(ctx, db, doc.ID); err != nil {
			return "", err
		}
		return "skipped", nil
	}
	if _, _, err := PromoteDocument(ctx, db, src, doc, e); err != nil {
		return recordFailure(ctx, db, src, doc, doc.ArchivedURL, errTypeParse, err)
	}
	return "extracted", nil
}

// recordFailure delegates the failure write to the source (which marks the row
// failed, bumps the attempt counter, and logs a crawl_errors row with errType).
// Returns status "failed".
func recordFailure(ctx context.Context, db *sql.DB, src StagedDocSource, doc ExtractDoc, url, errType string, cause error) (string, error) {
	if err := src.RecordFailure(ctx, db, doc, url, errType, cause); err != nil {
		return "", err
	}
	return "failed", nil
}

// PersistOCRText writes text to <storeDir>/<iso2>/<digest>.txt and records the
// path on the staged row (advancing it to 'ocr_done') via the source. Returns
// the text path.
func PersistOCRText(ctx context.Context, db *sql.DB, src StagedDocSource, storeDir, iso2, digest string, docID int64, text string) (string, error) {
	dir := filepath.Join(storeDir, iso2)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", err
	}
	path := filepath.Join(dir, digest+".txt")
	if err := os.WriteFile(path, []byte(text), 0o644); err != nil {
		return "", err
	}
	if err := src.PersistOCRPath(ctx, db, docID, path); err != nil {
		return "", err
	}
	return path, nil
}

// ProcessExtractPending runs up to limit documents needing extraction across all
// sources, highest country priority first. limit <= 0 means no cap.
func ProcessExtractPending(ctx context.Context, db *sql.DB, ocr OCRClient, llm LLMClient, storeDir string, limit int, sources ...StagedDocSource) (ExtractStats, error) {
	type pending struct {
		src StagedDocSource
		doc ExtractDoc
	}
	var all []pending
	for _, src := range sources {
		docs, err := src.PendingDocs(ctx, db, limit)
		if err != nil {
			return ExtractStats{}, err
		}
		for _, d := range docs {
			all = append(all, pending{src: src, doc: d})
		}
	}
	// Merge sources by country priority (desc), then document id (asc) — the same
	// ordering each adapter applies internally. Stable so equal-priority keeps the
	// per-source order.
	sort.SliceStable(all, func(i, j int) bool {
		if all[i].doc.Priority != all[j].doc.Priority {
			return all[i].doc.Priority > all[j].doc.Priority
		}
		return all[i].doc.ID < all[j].doc.ID
	})
	if limit > 0 && len(all) > limit {
		all = all[:limit]
	}

	var stats ExtractStats
	for _, p := range all {
		status, err := extractOne(ctx, db, p.src, ocr, llm, storeDir, p.doc)
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
		}
	}
	return stats, nil
}

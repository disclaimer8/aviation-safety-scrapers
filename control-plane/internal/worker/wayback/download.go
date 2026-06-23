package wayback

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
)

// StagedDoc is a staged document awaiting download.
type StagedDoc struct {
	ID          int64
	ArchivedURL string
	Digest      string
}

// PendingDocs returns the country's staged documents still pending download.
func PendingDocs(ctx context.Context, db *sql.DB, countryID int64) ([]StagedDoc, error) {
	rows, err := db.QueryContext(ctx, `
		SELECT id, archived_url, digest FROM staged_wayback_documents
		 WHERE country_id = ? AND download_status = 'pending'
		 ORDER BY id ASC`, countryID)
	if err != nil {
		return nil, fmt.Errorf("wayback: pending docs %d: %w", countryID, err)
	}
	defer rows.Close()
	var out []StagedDoc
	for rows.Next() {
		var d StagedDoc
		if err := rows.Scan(&d.ID, &d.ArchivedURL, &d.Digest); err != nil {
			return nil, fmt.Errorf("wayback: scan pending doc: %w", err)
		}
		out = append(out, d)
	}
	return out, rows.Err()
}

// DownloadStaged fetches one staged document, writes it under
// <storeDir>/<iso2>/<digest>.pdf, records the checksum, and marks it downloaded.
// On failure it marks the row failed and returns the error.
func DownloadStaged(ctx context.Context, db *sql.DB, f Fetcher, storeDir, iso2 string, doc StagedDoc) error {
	body, err := f.Get(ctx, doc.ArchivedURL)
	if err != nil {
		markFailed(ctx, db, doc.ID)
		return fmt.Errorf("wayback: download %s: %w", doc.ArchivedURL, err)
	}
	dir := filepath.Join(storeDir, iso2)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		markFailed(ctx, db, doc.ID)
		return fmt.Errorf("wayback: mkdir %s: %w", dir, err)
	}
	destPath := filepath.Join(dir, doc.Digest+".pdf")
	if err := os.WriteFile(destPath, body, 0o644); err != nil {
		markFailed(ctx, db, doc.ID)
		return fmt.Errorf("wayback: write %s: %w", destPath, err)
	}
	sum := sha256.Sum256(body)
	checksum := hex.EncodeToString(sum[:])
	if _, err := db.ExecContext(ctx, `
		UPDATE staged_wayback_documents
		   SET local_file_path = ?, checksum = ?, download_status = 'downloaded'
		 WHERE id = ?`, destPath, checksum, doc.ID); err != nil {
		return fmt.Errorf("wayback: mark downloaded %d: %w", doc.ID, err)
	}
	return nil
}

func markFailed(ctx context.Context, db *sql.DB, id int64) {
	_, _ = db.ExecContext(ctx,
		`UPDATE staged_wayback_documents SET download_status = 'failed' WHERE id = ?`, id)
}

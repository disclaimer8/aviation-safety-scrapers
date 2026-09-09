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

// PendingDocs returns the country's staged documents still awaiting download.
//
// 'failed' is included deliberately. It used to select 'pending' only, and
// nothing ever re-created the row: staging is ON CONFLICT DO NOTHING, so one
// transient archive.org 5xx dropped that capture permanently. The regional,
// foreign and manufacturer sources all retry their failed downloads; wayback
// was the only one that did not.
func PendingDocs(ctx context.Context, db *sql.DB, countryID int64) ([]StagedDoc, error) {
	rows, err := db.QueryContext(ctx, `
		SELECT id, archived_url, digest FROM staged_wayback_documents
		 WHERE country_id = ? AND download_status IN ('pending','failed')
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
	// The digest comes off the CDX API, not from us. filepath.Join cleans the
	// result, so a digest containing ".." would resolve outside store-dir.
	name, err := safeDigestFilename(doc.Digest)
	if err != nil {
		markFailed(ctx, db, doc.ID)
		return fmt.Errorf("wayback: download %s: %w", doc.ArchivedURL, err)
	}
	destPath := filepath.Join(dir, name)
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

// safeDigestFilename turns a CDX digest into a filename, refusing anything
// that is not a plain base32/hex token. Wayback digests are base32 SHA-1, so
// this rejects only malformed or hostile values.
func safeDigestFilename(digest string) (string, error) {
	if digest == "" {
		return "", fmt.Errorf("empty digest")
	}
	for _, r := range digest {
		ok := (r >= 'A' && r <= 'Z') || (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9')
		if !ok {
			return "", fmt.Errorf("digest %q is not a plain alphanumeric token", digest)
		}
	}
	return digest + ".pdf", nil
}

func markFailed(ctx context.Context, db *sql.DB, id int64) {
	_, _ = db.ExecContext(ctx,
		`UPDATE staged_wayback_documents SET download_status = 'failed' WHERE id = ?`, id)
}

package wayback

import (
	"context"
	"database/sql"
	"fmt"
)

// StageSnapshots inserts each snapshot into staged_wayback_documents, skipping
// any (country_id, digest) already present. Returns the count newly inserted.
func StageSnapshots(ctx context.Context, db *sql.DB, jobID, countryID int64, snaps []Snapshot) (int, error) {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return 0, fmt.Errorf("wayback: stage begin tx: %w", err)
	}
	defer tx.Rollback()

	stmt, err := tx.PrepareContext(ctx, `
		INSERT INTO staged_wayback_documents
			(crawl_job_id, country_id, original_url, archived_url, timestamp, mimetype, digest, length)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(country_id, digest) DO NOTHING`)
	if err != nil {
		return 0, fmt.Errorf("wayback: stage prepare: %w", err)
	}
	defer stmt.Close()

	staged := 0
	for _, s := range snaps {
		res, err := stmt.ExecContext(ctx, jobID, countryID, s.OriginalURL, s.ArchivedURL,
			s.Timestamp, s.Mimetype, s.Digest, s.Length)
		if err != nil {
			return 0, fmt.Errorf("wayback: stage insert %s: %w", s.Digest, err)
		}
		n, _ := res.RowsAffected()
		staged += int(n)
	}
	if err := tx.Commit(); err != nil {
		return 0, fmt.Errorf("wayback: stage commit: %w", err)
	}
	return staged, nil
}

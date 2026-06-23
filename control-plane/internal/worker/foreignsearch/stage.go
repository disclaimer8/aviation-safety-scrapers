package foreignsearch

import (
	"context"
	"database/sql"
	"fmt"
)

// StageRecords inserts each record into staged_foreign_documents, skipping any
// (authority, foreign_ref) already present. Returns the count newly inserted.
func StageRecords(ctx context.Context, db *sql.DB, jobID, countryID int64, authority string, recs []ForeignRecord) (int, error) {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return 0, fmt.Errorf("foreignsearch: stage begin tx: %w", err)
	}
	defer tx.Rollback()

	stmt, err := tx.PrepareContext(ctx, `
		INSERT INTO staged_foreign_documents
			(crawl_job_id, country_id, authority, foreign_ref, title, occurrence_date, original_url, report_url, mimetype)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(authority, foreign_ref) DO NOTHING`)
	if err != nil {
		return 0, fmt.Errorf("foreignsearch: stage prepare: %w", err)
	}
	defer stmt.Close()

	staged := 0
	for _, r := range recs {
		occ := nullIfEmpty(r.OccurrenceDate)
		rep := nullIfEmpty(r.ReportURL)
		mime := nullIfEmpty(r.Mimetype)
		res, err := stmt.ExecContext(ctx, jobID, countryID, authority, r.ForeignRef, r.Title, occ, r.OriginalURL, rep, mime)
		if err != nil {
			return 0, fmt.Errorf("foreignsearch: stage insert %s/%s: %w", authority, r.ForeignRef, err)
		}
		n, _ := res.RowsAffected()
		staged += int(n)
	}
	if err := tx.Commit(); err != nil {
		return 0, fmt.Errorf("foreignsearch: stage commit: %w", err)
	}
	return staged, nil
}

func nullIfEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}

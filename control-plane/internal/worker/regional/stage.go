package regional

import (
	"context"
	"database/sql"
	"fmt"
)

// StageRecords inserts each record into staged_regional_documents, skipping any
// (body_code, ref) already present. Returns the count newly inserted.
func StageRecords(ctx context.Context, db *sql.DB, jobID, countryID int64, bodyCode string, recs []RegionalRecord) (int, error) {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return 0, fmt.Errorf("regional: stage begin tx: %w", err)
	}
	defer tx.Rollback()
	stmt, err := tx.PrepareContext(ctx, `
		INSERT INTO staged_regional_documents
			(crawl_job_id, country_id, body_code, ref, title, occurrence_date, original_url, report_url, mimetype)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(body_code, ref) DO NOTHING`)
	if err != nil {
		return 0, fmt.Errorf("regional: stage prepare: %w", err)
	}
	defer stmt.Close()
	staged := 0
	for _, r := range recs {
		res, err := stmt.ExecContext(ctx, jobID, countryID, bodyCode, r.Ref, r.Title,
			nullIfEmpty(r.OccurrenceDate), r.OriginalURL, nullIfEmpty(r.ReportURL), nullIfEmpty(r.Mimetype))
		if err != nil {
			return 0, fmt.Errorf("regional: stage insert %s/%s: %w", bodyCode, r.Ref, err)
		}
		n, _ := res.RowsAffected()
		staged += int(n)
	}
	if err := tx.Commit(); err != nil {
		return 0, fmt.Errorf("regional: stage commit: %w", err)
	}
	return staged, nil
}

func nullIfEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}

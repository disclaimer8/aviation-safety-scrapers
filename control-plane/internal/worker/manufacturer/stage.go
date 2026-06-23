package manufacturer

import (
	"context"
	"database/sql"
	"fmt"
)

// StageRecords inserts each record into staged_manufacturer_documents, skipping any
// (publication, issue_ref) already present. Returns the count newly inserted.
func StageRecords(ctx context.Context, db *sql.DB, manufacturer, publication string, recs []ManufacturerRecord) (staged int, err error) {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return 0, fmt.Errorf("manufacturer: stage begin tx: %w", err)
	}
	defer tx.Rollback()
	stmt, err := tx.PrepareContext(ctx, `
		INSERT INTO staged_manufacturer_documents
			(manufacturer, publication, issue_ref, title, publication_date, original_url, report_url)
		VALUES (?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(publication, issue_ref) DO NOTHING`)
	if err != nil {
		return 0, fmt.Errorf("manufacturer: stage prepare: %w", err)
	}
	defer stmt.Close()
	for _, r := range recs {
		res, err := stmt.ExecContext(ctx, manufacturer, publication, r.IssueRef, r.Title,
			nullIfEmpty(r.PublicationDate), r.OriginalURL, nullIfEmpty(r.ReportURL))
		if err != nil {
			return 0, fmt.Errorf("manufacturer: stage insert %s/%s: %w", publication, r.IssueRef, err)
		}
		n, _ := res.RowsAffected()
		staged += int(n)
	}
	if err := tx.Commit(); err != nil {
		return 0, fmt.Errorf("manufacturer: stage commit: %w", err)
	}
	return staged, nil
}

func nullIfEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}

package foreignsearch

import (
	"context"
	"database/sql"
	"fmt"
)

// StageRecords inserts each record into staged_foreign_documents, skipping any
// (authority, foreign_ref) already present. Returns the count newly inserted.
//
// countryID<=0 stages every record with country_id NULL rather than a false 0
// (which would violate the FK). RunJob passes 0 for authorities whose listing
// is body-wide and not filtered per country (currently: bea — see bea.go's
// Search doc comment) so it never stamps the wrong country (GO-CP-1); NTSB and
// ATSB are genuinely filtered per country (CAROL query param / pre-filtered
// source file respectively) and pass the real job country as before.
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
		res, err := stmt.ExecContext(ctx, jobID, nullIfZero(countryID), authority, r.ForeignRef, r.Title, occ, r.OriginalURL, rep, mime)
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

// nullIfZero returns nil for a non-positive id so a body-wide job's
// country-less staging call writes NULL instead of a false 0, which would
// violate the countries FK; a country-driven caller always passes a real id.
func nullIfZero(id int64) any {
	if id <= 0 {
		return nil
	}
	return id
}

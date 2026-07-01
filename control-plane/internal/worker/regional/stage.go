package regional

import (
	"context"
	"database/sql"
	"fmt"
	"strings"
)

// StageRecords inserts each record into staged_regional_documents, skipping any
// (body_code, ref) already present. Returns the count newly inserted.
//
// Every regional body currently wired (ECCAA/BAGAIA/IAC) publishes ONE body-wide
// listing that is not filtered per member country (see iac.go/eccaa.go/bagaia.go
// Search doc comments) — a single job run stages every accident the body has
// ever published. Stamping all of them with the crawling job's country
// (countryID) would misattribute every other member country's accidents to
// whichever country's job happened to run first; this was confirmed live (a
// Belarus accident and a Kazakh Mi-8 both stamped RU via IAC). Callers driving a
// body-wide job MUST therefore pass countryID<=0 so the row stages with NO
// country claim (NULL); the extract step resolves the true country later from
// the report content (see extract/promote.go).
//
// A record can still carry a deterministic per-record country
// (RegionalRecord.CountryISO2) when a future parser is able to read one from the
// listing itself (e.g. a member-state column). That value, when it resolves to a
// known ISO2, always wins over countryID — it is exact, not a job-scoping guess.
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
		cid, err := resolveRecordCountryID(ctx, tx, r.CountryISO2, countryID)
		if err != nil {
			return 0, fmt.Errorf("regional: resolve country for %s/%s: %w", bodyCode, r.Ref, err)
		}
		res, err := stmt.ExecContext(ctx, jobID, cid, bodyCode, r.Ref, r.Title,
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

// resolveRecordCountryID returns the country_id to stamp on a staged row: the
// record's own deterministic ISO2 when present and known, else fallback (which
// callers pass as <=0 for body-wide sources so it resolves to NULL). An unknown
// ISO2 (typo, or a code not in the countries table) falls back rather than
// erroring — a bad deterministic hint should not abort staging, and NULL is
// always safer than a wrong guess.
func resolveRecordCountryID(ctx context.Context, tx *sql.Tx, iso2 string, fallback int64) (any, error) {
	iso2 = strings.ToUpper(strings.TrimSpace(iso2))
	if len(iso2) == 2 {
		var id int64
		err := tx.QueryRowContext(ctx, `SELECT id FROM countries WHERE iso2 = ?`, iso2).Scan(&id)
		if err == nil {
			return id, nil
		}
		if err != sql.ErrNoRows {
			return nil, err
		}
	}
	return nullIfZero(fallback), nil
}

func nullIfEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}

// nullIfZero returns nil for a non-positive id so a body-wide job's
// country-less staging call (countryID<=0) writes NULL instead of a false 0,
// which would violate the FK; a country-driven caller always passes a real id.
func nullIfZero(id int64) any {
	if id <= 0 {
		return nil
	}
	return id
}

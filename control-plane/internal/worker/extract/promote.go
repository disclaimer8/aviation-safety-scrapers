package extract

import (
	"context"
	"database/sql"
	"fmt"
	"strings"
)

// upsertSource looks-up-or-creates a source keyed on UNIQUE(canonical_url,
// source_type) and returns its id. Adapters reuse it from ResolveSource.
func upsertSource(ctx context.Context, q execQuerier, name, url, canonical, sourceType string, tier int) (int64, error) {
	if _, err := q.ExecContext(ctx, `
		INSERT INTO sources (name, url, canonical_url, source_type, source_tier)
		VALUES (?, ?, ?, ?, ?)
		ON CONFLICT(canonical_url, source_type) DO NOTHING`,
		name, url, canonical, sourceType, tier); err != nil {
		return 0, fmt.Errorf("extract: upsert source %s: %w", canonical, err)
	}
	var id int64
	if err := q.QueryRowContext(ctx, `
		SELECT id FROM sources WHERE canonical_url = ? AND source_type = ?`,
		canonical, sourceType).Scan(&id); err != nil {
		return 0, fmt.Errorf("extract: select source %s: %w", canonical, err)
	}
	return id, nil
}

// normalizeReg upper-cases and trims an aircraft registration for comparison.
func normalizeReg(s string) string {
	return strings.ToUpper(strings.TrimSpace(s))
}

// PromoteDocument inserts or links an event, inserts a report, and advances the
// staged document to 'extracted' — all in ONE transaction (via the source's
// MarkExtractedTx). A crash before commit rolls everything back, so the doc is
// re-selected next run rather than leaving an extracted event with the staged row
// still pending (which would re-promote and duplicate). Returns the event id and
// whether it linked to an existing event.
func PromoteDocument(ctx context.Context, db *sql.DB, src StagedDocSource, doc ExtractDoc, e ExtractedEvent) (int64, bool, error) {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return 0, false, fmt.Errorf("extract: promote begin tx: %w", err)
	}
	defer tx.Rollback()

	sourceID, tier, copyright, err := src.ResolveSource(ctx, tx, doc)
	if err != nil {
		return 0, false, err
	}
	official := tier == 1

	eventID, linked, err := FindDuplicateEvent(ctx, tx, e)
	if err != nil {
		return 0, false, err
	}
	if linked {
		if _, err := tx.ExecContext(ctx, `
			UPDATE events SET dedup_status='soft_linked', updated_at=unixepoch('subsec')*1000
			 WHERE id=? AND dedup_status='unreviewed'`, eventID); err != nil {
			return 0, false, fmt.Errorf("extract: soft-link event %d: %w", eventID, err)
		}
	} else {
		occCountryID, err := resolveOccurrenceCountryID(ctx, tx, doc, e)
		if err != nil {
			return 0, false, err
		}
		conf := ConfidenceScore(e, official)
		res, err := tx.ExecContext(ctx, `
			INSERT INTO events
				(date, date_precision, occurrence_country_id, location, latitude, longitude,
				 aircraft_registration, aircraft_type, manufacturer, operator_name, flight_number,
				 fatalities, injuries, event_type, investigation_status, confidence_score, dedup_status)
			VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'unreviewed')`,
			nullStr(e.Date), e.DatePrecision, nullInt64(occCountryID), nullStr(e.Location), e.Latitude, e.Longitude,
			nullStr(e.AircraftRegistration), nullStr(e.AircraftType), nullStr(e.Manufacturer),
			nullStr(e.OperatorName), nullStr(e.FlightNumber), e.Fatalities, e.Injuries,
			e.EventType, e.InvestigationStatus, conf)
		if err != nil {
			return 0, false, fmt.Errorf("extract: insert event: %w", err)
		}
		eventID, _ = res.LastInsertId()
	}

	title := e.Title
	if title == "" {
		title = doc.ISO2 + " accident report"
	}
	language := e.Language
	if language == "" {
		language = "en"
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO reports
			(event_id, source_id, report_type, title, language, original_url, archived_url, pdf_url,
			 published_date, accessed_at, checksum, local_file_path, source_tier, extraction_status, copyright_status)
		VALUES (?,?,?,?,?,?,?,?,?, unixepoch('subsec')*1000, ?, ?, ?, 'extracted', ?)`,
		eventID, sourceID, e.ReportType, title, language, doc.OriginalURL, doc.ArchivedURL, doc.OriginalURL,
		nullStr(e.PublishedDate), doc.Checksum, doc.LocalFilePath, tier, copyright); err != nil {
		return 0, false, fmt.Errorf("extract: insert report: %w", err)
	}

	if err := src.MarkExtractedTx(ctx, tx, doc.ID, eventID); err != nil {
		return 0, false, err
	}

	if err := tx.Commit(); err != nil {
		return 0, false, fmt.Errorf("extract: promote commit: %w", err)
	}
	return eventID, linked, nil
}

// nullStr returns nil for an empty string so an empty optional column stays NULL.
func nullStr(s string) any {
	if s == "" {
		return nil
	}
	return s
}

// nullInt64 returns nil for a non-positive id so an absent foreign key (e.g. a
// country-less manufacturer document, doc.CountryID==0) writes NULL instead of 0,
// which would violate the events.occurrence_country_id REFERENCES countries(id)
// constraint. Country-driven sources always pass a real id and are unaffected.
func nullInt64(i int64) any {
	if i <= 0 {
		return nil
	}
	return i
}

// resolveOccurrenceCountryID decides the country_id to stamp on a NEW event
// (GO-CP-1). doc.CountryID already reflects a deterministic attribution — a
// per-country source (wayback, foreign-search for NTSB/ATSB, manufacturer
// docs which are correctly country-less) or a regional record whose listing
// carried its own per-record country (regional.RegionalRecord.CountryISO2,
// resolved at stage time) — so it always wins over the LLM when set.
//
// When doc.CountryID is 0 (the body-wide-listing case: ECCAA/BAGAIA/IAC/BEA
// stage with no country claim — see regional/stage.go and
// foreignsearch/runner.go), the country is instead resolved from what the LLM
// read out of the report content itself (ExtractedEvent.Country, normalized to
// an ISO2 or ""). An empty or unmappable code leaves occurrence_country_id
// NULL — guessing a country is exactly the bug this fixes, so an unresolved
// country must stay unresolved rather than default to anything.
func resolveOccurrenceCountryID(ctx context.Context, q execQuerier, doc ExtractDoc, e ExtractedEvent) (int64, error) {
	if doc.CountryID > 0 {
		return doc.CountryID, nil
	}
	iso2 := normalizeISO2(e.Country)
	if iso2 == "" {
		return 0, nil
	}
	var id int64
	err := q.QueryRowContext(ctx, `SELECT id FROM countries WHERE iso2 = ?`, iso2).Scan(&id)
	if err == sql.ErrNoRows {
		return 0, nil
	}
	if err != nil {
		return 0, fmt.Errorf("extract: resolve occurrence country %q: %w", e.Country, err)
	}
	return id, nil
}

// FindDuplicateEvent looks for an existing event that is the same occurrence.
// Key 1 (when the candidate has a registration): same exact date AND same
// normalized registration. Key 2 (when registration is absent): same exact date
// AND same operator AND same fatalities. Only exact-precision candidate dates
// participate.
func FindDuplicateEvent(ctx context.Context, q execQuerier, e ExtractedEvent) (int64, bool, error) {
	if e.DatePrecision != "exact" || e.Date == "" {
		return 0, false, nil
	}
	reg := normalizeReg(e.AircraftRegistration)
	if reg != "" {
		var id int64
		err := q.QueryRowContext(ctx, `
			SELECT id FROM events
			 WHERE date = ? AND upper(trim(aircraft_registration)) = ?
			 ORDER BY id ASC LIMIT 1`, e.Date, reg).Scan(&id)
		if err == sql.ErrNoRows {
			return 0, false, nil
		}
		if err != nil {
			return 0, false, fmt.Errorf("extract: dedup key1: %w", err)
		}
		return id, true, nil
	}
	if e.OperatorName != "" && e.Fatalities != nil {
		var id int64
		err := q.QueryRowContext(ctx, `
			SELECT id FROM events
			 WHERE date = ? AND operator_name = ? AND fatalities = ?
			 ORDER BY id ASC LIMIT 1`, e.Date, e.OperatorName, *e.Fatalities).Scan(&id)
		if err == sql.ErrNoRows {
			return 0, false, nil
		}
		if err != nil {
			return 0, false, fmt.Errorf("extract: dedup key2: %w", err)
		}
		return id, true, nil
	}
	return 0, false, nil
}

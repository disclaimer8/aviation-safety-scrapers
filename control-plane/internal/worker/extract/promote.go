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

// normalizeReg upper-cases and trims an aircraft registration for comparison
// and for storage (dashes are kept intact — project convention, see
// reference_aircraft-livery-tracking.md: "reg=UPPER keep-dashes").
func normalizeReg(s string) string {
	return strings.ToUpper(strings.TrimSpace(s))
}

// placeholderRegs is the set of normalized (normalizeReg'd) values that stand
// in for "no registration known" rather than a real tail number. Live regional
// data includes the Cyrillic "б/н" ("bez nomera" / without number) placeholder
// alongside the usual English variants (GO-CP-5b). Matched case-insensitively
// via normalizeReg before lookup, so this map only needs the upper-cased form.
var placeholderRegs = map[string]bool{
	"":        true,
	"N/A":     true,
	"NA":      true,
	"N.A.":    true,
	"UNKNOWN": true,
	"UNK":     true,
	"NONE":    true,
	"NIL":     true,
	"-":       true,
	"—":       true,
	"Б/Н":     true, // Cyrillic "without number"
	"БН":      true,
}

// isPlaceholderReg reports whether a normalizeReg'd registration is a known
// placeholder (not a real tail number) and must not participate in dedup
// key-1 matching — otherwise every "б/н"/"N/A" record in the same body-wide
// listing would collapse onto one event.
func isPlaceholderReg(normalized string) bool {
	return placeholderRegs[normalized]
}

// matchesNonEmpty reports whether a and b are both non-empty and equal after
// trimming, case-insensitively. Used to corroborate a weak dedup key-2 match
// (date+operator+fatalities) against aircraft_type or location before
// treating it as the same occurrence.
func matchesNonEmpty(a, b string) bool {
	a = strings.TrimSpace(a)
	b = strings.TrimSpace(b)
	if a == "" || b == "" {
		return false
	}
	return strings.EqualFold(a, b)
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

	eventID, linked, needsReview, err := FindDuplicateEvent(ctx, tx, e)
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
		// A weak key-2 match (GO-CP-5c) is inserted as its own event, not
		// auto-linked, but flagged for a human to resolve rather than
		// defaulting to 'unreviewed' as if no candidate had been seen at all.
		dedupStatus := "unreviewed"
		if needsReview {
			dedupStatus = "manual_review"
		}
		res, err := tx.ExecContext(ctx, `
			INSERT INTO events
				(date, date_precision, occurrence_country_id, location, latitude, longitude,
				 aircraft_registration, aircraft_type, manufacturer, operator_name, flight_number,
				 fatalities, injuries, event_type, investigation_status, confidence_score, dedup_status)
			VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
			nullStr(e.Date), e.DatePrecision, nullInt64(occCountryID), nullStr(e.Location), e.Latitude, e.Longitude,
			nullStr(normalizeReg(e.AircraftRegistration)), nullStr(e.AircraftType), nullStr(e.Manufacturer),
			nullStr(e.OperatorName), nullStr(e.FlightNumber), e.Fatalities, e.Injuries,
			e.EventType, e.InvestigationStatus, conf, dedupStatus)
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
//
// Key 1 (when the candidate has a real registration — placeholders like
// "N/A"/"б/н" don't count, see isPlaceholderReg): same exact date AND same
// normalized registration. A key-1 match is always confident enough to
// auto-link (a registration collision on the same day is the same aircraft).
//
// Key 2 (when registration is absent or a placeholder): same exact date AND
// same operator AND same fatalities. This alone is too weak to auto-link —
// two distinct same-day accidents at the same operator with equal fatalities
// happen (e.g. GO-CP-5c: a body-wide regional listing mixing two unrelated
// occurrences) — so it additionally requires aircraft_type OR location to
// corroborate. A date+operator+fatalities match WITHOUT that corroboration is
// returned as needsReview=true instead of being auto-linked: the caller must
// insert a new event and flag it for manual dedup review (dedup_status=
// 'manual_review' — the schema's closest fit to "needs review"; there is no
// separate 'needs_review' enum value) rather than silently merging two
// possibly-distinct events, which would drop the second event's own record.
//
// Only exact-precision candidate dates participate.
func FindDuplicateEvent(ctx context.Context, q execQuerier, e ExtractedEvent) (eventID int64, linked bool, needsReview bool, err error) {
	if e.DatePrecision != "exact" || e.Date == "" {
		return 0, false, false, nil
	}
	reg := normalizeReg(e.AircraftRegistration)
	if reg != "" && !isPlaceholderReg(reg) {
		var id int64
		err := q.QueryRowContext(ctx, `
			SELECT id FROM events
			 WHERE date = ? AND upper(trim(aircraft_registration)) = ?
			 ORDER BY id ASC LIMIT 1`, e.Date, reg).Scan(&id)
		if err == sql.ErrNoRows {
			return 0, false, false, nil
		}
		if err != nil {
			return 0, false, false, fmt.Errorf("extract: dedup key1: %w", err)
		}
		return id, true, false, nil
	}
	if e.OperatorName != "" && e.Fatalities != nil {
		var id int64
		var dbAircraftType, dbLocation sql.NullString
		err := q.QueryRowContext(ctx, `
			SELECT id, aircraft_type, location FROM events
			 WHERE date = ? AND operator_name = ? AND fatalities = ?
			 ORDER BY id ASC LIMIT 1`, e.Date, e.OperatorName, *e.Fatalities).Scan(&id, &dbAircraftType, &dbLocation)
		if err == sql.ErrNoRows {
			return 0, false, false, nil
		}
		if err != nil {
			return 0, false, false, fmt.Errorf("extract: dedup key2: %w", err)
		}
		if matchesNonEmpty(dbAircraftType.String, e.AircraftType) || matchesNonEmpty(dbLocation.String, e.Location) {
			return id, true, false, nil
		}
		return 0, false, true, nil
	}
	return 0, false, false, nil
}

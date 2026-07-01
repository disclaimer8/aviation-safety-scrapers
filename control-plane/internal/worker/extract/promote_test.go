package extract

import (
	"context"
	"database/sql"
	"testing"
)

func loadDoc(t *testing.T, db *sql.DB, docID int64) ExtractDoc {
	t.Helper()
	var d ExtractDoc
	d.ID = docID
	err := db.QueryRowContext(context.Background(), `
		SELECT d.country_id, c.iso2, d.digest, d.local_file_path, d.original_url, d.archived_url,
		       d.ocr_text_path, d.checksum, coalesce(c.wayback_target,'')
		  FROM staged_wayback_documents d JOIN countries c ON c.id=d.country_id
		 WHERE d.id=?`, docID).
		Scan(&d.CountryID, &d.ISO2, &d.Digest, &d.LocalFilePath, &d.OriginalURL, &d.ArchivedURL,
			&d.OCRTextPath, &d.Checksum, &d.WaybackTarget)
	if err != nil {
		t.Fatal(err)
	}
	return d
}

func TestPromoteDocumentNewEvent(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID, _ := seedDownloadedDoc(t, db, "KE", "k1")
	doc := loadDoc(t, db, docID)

	e := NormalizeEvent(ExtractedEvent{
		IsAviationAccident: true, Date: "2019-03-10", DatePrecision: "exact",
		Location: "Bishoftu", AircraftRegistration: "ET-AVJ", AircraftType: "B738",
		OperatorName: "Ethiopian", Fatalities: intp(157), EventType: "accident",
		ReportType: "final", Title: "Final Report", Language: "en",
	})
	eventID, linked, err := PromoteDocument(ctx, db, WaybackSource{}, doc, e)
	if err != nil {
		t.Fatalf("PromoteDocument: %v", err)
	}
	if eventID == 0 || linked {
		t.Fatalf("want new event, got id=%d linked=%v", eventID, linked)
	}
	// events row: confidence, dedup_status.
	var conf int
	var dedup string
	db.QueryRowContext(ctx, `SELECT confidence_score, dedup_status FROM events WHERE id=?`, eventID).Scan(&conf, &dedup)
	if conf == 0 || dedup != "unreviewed" {
		t.Fatalf("event conf=%d dedup=%q", conf, dedup)
	}
	// reports row: archived_url + copyright_status.
	var arch, cr string
	db.QueryRowContext(ctx, `SELECT archived_url, copyright_status FROM reports WHERE event_id=?`, eventID).Scan(&arch, &cr)
	if arch != doc.ArchivedURL || cr == "" {
		t.Fatalf("report arch=%q cr=%q", arch, cr)
	}
	// staged doc advanced.
	var status string
	var linkedEvent sql.NullInt64
	db.QueryRowContext(ctx, `SELECT extraction_status, event_id FROM staged_wayback_documents WHERE id=?`, docID).Scan(&status, &linkedEvent)
	if status != "extracted" || !linkedEvent.Valid || linkedEvent.Int64 != eventID {
		t.Fatalf("doc status=%q event_id=%v", status, linkedEvent)
	}
}

func TestPromoteDocumentLinksDuplicate(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	existing := insertEvent(t, db, "2019-03-10", "ET-AVJ", "Ethiopian", intp(157))
	docID, _ := seedDownloadedDoc(t, db, "KE", "k2")
	doc := loadDoc(t, db, docID)

	e := NormalizeEvent(ExtractedEvent{
		IsAviationAccident: true, Date: "2019-03-10", DatePrecision: "exact",
		AircraftRegistration: "ET-AVJ", AircraftType: "B738", ReportType: "final", Language: "en",
	})
	eventID, linked, err := PromoteDocument(ctx, db, WaybackSource{}, doc, e)
	if err != nil {
		t.Fatalf("PromoteDocument: %v", err)
	}
	if !linked || eventID != existing {
		t.Fatalf("want link to %d, got id=%d linked=%v", existing, eventID, linked)
	}
	var dedup string
	db.QueryRowContext(ctx, `SELECT dedup_status FROM events WHERE id=?`, existing).Scan(&dedup)
	if dedup != "soft_linked" {
		t.Fatalf("dedup_status=%q want soft_linked", dedup)
	}
	// No second event created.
	var n int
	db.QueryRowContext(ctx, `SELECT count(*) FROM events`).Scan(&n)
	if n != 1 {
		t.Fatalf("event count=%d want 1", n)
	}
	// Report row created for linked event.
	var reportCount int
	if err := db.QueryRowContext(ctx, `SELECT count(*) FROM reports WHERE event_id=?`, existing).Scan(&reportCount); err != nil {
		t.Fatalf("count reports: %v", err)
	}
	if reportCount != 1 {
		t.Fatalf("want 1 report for linked event, got %d", reportCount)
	}
}

// ─── resolveOccurrenceCountryID (GO-CP-1) ───────────────────────────────────
//
// These are the promote-time counterpart to the stage-time GO-CP-1 fix: a
// body-wide staged doc (doc.CountryID==0, e.g. IAC/ECCAA/BAGAIA/BEA) must NOT
// inherit the crawling job's country; instead the occurrence country is
// resolved from what the LLM read in the report content, and left NULL when
// that is empty or unmappable rather than guessed.

func TestResolveOccurrenceCountryIDPrefersDocCountryOverLLM(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	_, countryID := seedDownloadedDoc(t, db, "KE", "k1")

	// doc.CountryID is set (a genuinely country-driven source) — the LLM's
	// Country must be ignored even when it names a different, valid country.
	doc := ExtractDoc{CountryID: countryID}
	got, err := resolveOccurrenceCountryID(ctx, db, doc, ExtractedEvent{Country: "US"})
	if err != nil {
		t.Fatalf("resolveOccurrenceCountryID: %v", err)
	}
	if got != countryID {
		t.Fatalf("got=%d want doc.CountryID=%d (deterministic attribution must win over LLM)", got, countryID)
	}
}

func TestResolveOccurrenceCountryIDResolvesFromLLMWhenDocCountryless(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	// Seed BY as a real country (distinct from any doc/job country) so the
	// LLM-reported code can resolve to a real id.
	res, err := db.ExecContext(ctx, `
		INSERT INTO countries (iso2, iso3, name, region, policy_status, coverage_status, coverage_score, effort_score)
		VALUES ('BY','BLR','Belarus','Europe','allowed','regional_raio',2,3)`)
	if err != nil {
		t.Fatal(err)
	}
	byID, _ := res.LastInsertId()

	doc := ExtractDoc{CountryID: 0} // body-wide staged doc — no country claim.
	got, err := resolveOccurrenceCountryID(ctx, db, doc, ExtractedEvent{Country: "BY"})
	if err != nil {
		t.Fatalf("resolveOccurrenceCountryID: %v", err)
	}
	if got != byID {
		t.Fatalf("got=%d want BY id=%d", got, byID)
	}
}

func TestResolveOccurrenceCountryIDUnmappableLeavesNull(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	doc := ExtractDoc{CountryID: 0}

	for _, tc := range []struct {
		name    string
		country string
	}{
		{"empty", ""},
		{"valid-shaped code not in countries table", "ZZ"},
		{"country name instead of ISO2", "Belarus"},
		{"three-letter code", "BLR"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := resolveOccurrenceCountryID(ctx, db, doc, ExtractedEvent{Country: tc.country})
			if err != nil {
				t.Fatalf("resolveOccurrenceCountryID: %v", err)
			}
			if got != 0 {
				t.Fatalf("%s: got=%d want 0 (NULL) — must not guess", tc.name, got)
			}
		})
	}
}

// TestPromoteDocumentCountryLessDocResolvesFromLLM drives the full
// PromoteDocument path (not just the helper) for a body-wide-listing doc: the
// staged row's CountryID is 0, and the new event's occurrence_country_id must
// come from ExtractedEvent.Country.
func TestPromoteDocumentCountryLessDocResolvesFromLLM(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	res, err := db.ExecContext(ctx, `
		INSERT INTO countries (iso2, iso3, name, region, policy_status, coverage_status, coverage_score, effort_score)
		VALUES ('KZ','KAZ','Kazakhstan','Asia','allowed','regional_raio',2,3)`)
	if err != nil {
		t.Fatal(err)
	}
	kzID, _ := res.LastInsertId()

	// A country-less doc: CountryID==0 (as RegionalSource.PendingDocs now
	// returns for a NULL-country_id staged row), crediting IAC via SourceRef.
	doc := ExtractDoc{ID: 999, CountryID: 0, ISO2: "iac", SourceRef: "IAC"}
	// ResolveSource needs a regional_bodies row for SourceRef="IAC".
	if _, err := db.ExecContext(ctx, `
		INSERT INTO regional_bodies (code, name, body_class, website_url, source_url)
		VALUES ('IAC', 'IAC Body', 'regional_body', 'https://mak-iac.org', 'https://mak-iac.org/reports')`); err != nil {
		t.Fatal(err)
	}

	e := NormalizeEvent(ExtractedEvent{
		IsAviationAccident: true, Date: "2026-05-19", DatePrecision: "exact",
		AircraftRegistration: "UP-MI872", AircraftType: "Mi-8", Country: "kz", // lower-case, model output
		EventType: "accident", ReportType: "final", Language: "ru",
	})
	eventID, linked, err := PromoteDocument(ctx, db, RegionalSource{}, doc, e)
	if err != nil {
		t.Fatalf("PromoteDocument: %v", err)
	}
	if linked || eventID == 0 {
		t.Fatalf("want new event, got id=%d linked=%v", eventID, linked)
	}
	var gotCountryID sql.NullInt64
	db.QueryRowContext(ctx, `SELECT occurrence_country_id FROM events WHERE id=?`, eventID).Scan(&gotCountryID)
	if !gotCountryID.Valid || gotCountryID.Int64 != kzID {
		t.Fatalf("occurrence_country_id=%v want %d (KZ, resolved from LLM output on a country-less doc)", gotCountryID, kzID)
	}
}

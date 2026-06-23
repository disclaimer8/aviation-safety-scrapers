package wayback

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
	eventID, linked, err := PromoteDocument(ctx, db, doc, e)
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
	eventID, linked, err := PromoteDocument(ctx, db, doc, e)
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
}

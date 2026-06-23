package wayback

import (
	"context"
	"testing"
)

func insertEvent(t *testing.T, db execQuerier, date, reg, operator string, fatalities *int) int64 {
	t.Helper()
	res, err := db.ExecContext(context.Background(), `
		INSERT INTO events (date, date_precision, aircraft_registration, operator_name, fatalities, confidence_score)
		VALUES (?, 'exact', ?, ?, ?, 50)`, date, reg, operator, fatalities)
	if err != nil {
		t.Fatal(err)
	}
	id, _ := res.LastInsertId()
	return id
}

func TestFindDuplicateEventKey1(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	want := insertEvent(t, db, "2019-03-10", "ET-AVJ", "Ethiopian", intp(157))

	cand := ExtractedEvent{Date: "2019-03-10", DatePrecision: "exact", AircraftRegistration: "et-avj "}
	id, found, err := FindDuplicateEvent(ctx, db, cand)
	if err != nil || !found || id != want {
		t.Fatalf("key1 dedup: id=%d found=%v err=%v want %d", id, found, err, want)
	}
}

func TestFindDuplicateEventKey2(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	want := insertEvent(t, db, "2018-05-01", "", "AeroX", intp(3))

	// No registration on candidate -> key-2 (date+operator+fatalities).
	cand := ExtractedEvent{Date: "2018-05-01", DatePrecision: "exact", OperatorName: "AeroX", Fatalities: intp(3)}
	id, found, err := FindDuplicateEvent(ctx, db, cand)
	if err != nil || !found || id != want {
		t.Fatalf("key2 dedup: id=%d found=%v err=%v want %d", id, found, err, want)
	}
}

func TestFindDuplicateEventNoMatch(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	insertEvent(t, db, "2019-03-10", "ET-AVJ", "Ethiopian", intp(157))

	cand := ExtractedEvent{Date: "2020-01-01", DatePrecision: "exact", AircraftRegistration: "N12345"}
	_, found, err := FindDuplicateEvent(ctx, db, cand)
	if err != nil || found {
		t.Fatalf("expected no match: found=%v err=%v", found, err)
	}
}

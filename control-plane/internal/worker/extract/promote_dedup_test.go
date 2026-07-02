package extract

import (
	"context"
	"testing"
)

func insertEvent(t *testing.T, db execQuerier, date, reg, operator string, fatalities *int) int64 {
	t.Helper()
	return insertEventFull(t, db, date, reg, operator, fatalities, "", "")
}

func insertEventFull(t *testing.T, db execQuerier, date, reg, operator string, fatalities *int, aircraftType, location string) int64 {
	t.Helper()
	res, err := db.ExecContext(context.Background(), `
		INSERT INTO events (date, date_precision, aircraft_registration, operator_name, fatalities, aircraft_type, location, confidence_score)
		VALUES (?, 'exact', ?, ?, ?, ?, ?, 50)`, date, reg, operator, fatalities, aircraftType, location)
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
	id, linked, needsReview, err := FindDuplicateEvent(ctx, db, cand)
	if err != nil || !linked || needsReview || id != want {
		t.Fatalf("key1 dedup: id=%d linked=%v needsReview=%v err=%v want %d", id, linked, needsReview, err, want)
	}
}

// TestFindDuplicateEventKey1IgnoresDashCase mirrors the "reg=UPPER keep-dashes"
// project convention (GO-CP-5a): a dashed registration must still match
// regardless of the candidate's casing/whitespace, and dashes are preserved
// (not stripped) by normalization.
func TestFindDuplicateEventKey1IgnoresDashCase(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	want := insertEvent(t, db, "2020-06-01", "9M-MRO", "Malaysia", intp(0))

	cand := ExtractedEvent{Date: "2020-06-01", DatePrecision: "exact", AircraftRegistration: " 9m-mro "}
	id, linked, _, err := FindDuplicateEvent(ctx, db, cand)
	if err != nil || !linked || id != want {
		t.Fatalf("dashed reg dedup: id=%d linked=%v err=%v want %d", id, linked, err, want)
	}
}

// TestFindDuplicateEventKey2Corroborated verifies key-2 (date+operator+
// fatalities) auto-links when aircraft_type OR location also matches
// (GO-CP-5c: the stricter, corroborated path).
func TestFindDuplicateEventKey2Corroborated(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	want := insertEventFull(t, db, "2018-05-01", "", "AeroX", intp(3), "Boeing 737", "Lagos")

	cand := ExtractedEvent{Date: "2018-05-01", DatePrecision: "exact", OperatorName: "AeroX", Fatalities: intp(3), AircraftType: "boeing 737"}
	id, linked, needsReview, err := FindDuplicateEvent(ctx, db, cand)
	if err != nil || !linked || needsReview || id != want {
		t.Fatalf("key2 corroborated dedup: id=%d linked=%v needsReview=%v err=%v want %d", id, linked, needsReview, err, want)
	}
}

// TestFindDuplicateEventKey2CorroboratedByLocation is the same as above but
// corroborating via location instead of aircraft_type.
func TestFindDuplicateEventKey2CorroboratedByLocation(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	want := insertEventFull(t, db, "2018-05-01", "", "AeroX", intp(3), "", "Lagos")

	cand := ExtractedEvent{Date: "2018-05-01", DatePrecision: "exact", OperatorName: "AeroX", Fatalities: intp(3), Location: "  LAGOS  "}
	id, linked, needsReview, err := FindDuplicateEvent(ctx, db, cand)
	if err != nil || !linked || needsReview || id != want {
		t.Fatalf("key2 location-corroborated dedup: id=%d linked=%v needsReview=%v err=%v want %d", id, linked, needsReview, err, want)
	}
}

// TestFindDuplicateEventKey2UncorroboratedNeedsReview is GO-CP-5c's core
// regression guard: date+operator+fatalities alone (no aircraft_type/location
// corroboration) must NOT auto-link two possibly-distinct events — it must
// come back as needsReview so the caller inserts a separate event flagged for
// a human instead of silently merging.
func TestFindDuplicateEventKey2UncorroboratedNeedsReview(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	insertEvent(t, db, "2018-05-01", "", "AeroX", intp(3))

	cand := ExtractedEvent{Date: "2018-05-01", DatePrecision: "exact", OperatorName: "AeroX", Fatalities: intp(3)}
	id, linked, needsReview, err := FindDuplicateEvent(ctx, db, cand)
	if err != nil || linked || !needsReview || id != 0 {
		t.Fatalf("key2 uncorroborated: id=%d linked=%v needsReview=%v err=%v, want linked=false needsReview=true", id, linked, needsReview, err)
	}
}

// TestFindDuplicateEventKey2MismatchedCorroborationNeedsReview verifies that a
// disagreeing aircraft_type/location (not just a missing one) still routes to
// needsReview rather than being treated as a match.
func TestFindDuplicateEventKey2MismatchedCorroborationNeedsReview(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	insertEventFull(t, db, "2018-05-01", "", "AeroX", intp(3), "Boeing 737", "Lagos")

	cand := ExtractedEvent{Date: "2018-05-01", DatePrecision: "exact", OperatorName: "AeroX", Fatalities: intp(3),
		AircraftType: "Antonov An-24", Location: "Kano"}
	_, linked, needsReview, err := FindDuplicateEvent(ctx, db, cand)
	if err != nil || linked || !needsReview {
		t.Fatalf("mismatched corroboration: linked=%v needsReview=%v err=%v, want linked=false needsReview=true", linked, needsReview, err)
	}
}

func TestFindDuplicateEventNoMatch(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	insertEvent(t, db, "2019-03-10", "ET-AVJ", "Ethiopian", intp(157))

	cand := ExtractedEvent{Date: "2020-01-01", DatePrecision: "exact", AircraftRegistration: "N12345"}
	_, linked, needsReview, err := FindDuplicateEvent(ctx, db, cand)
	if err != nil || linked || needsReview {
		t.Fatalf("expected no match: linked=%v needsReview=%v err=%v", linked, needsReview, err)
	}
}

func TestFindDuplicateEventRegPresentSkipsKey2(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	// Stored row has a registration AND operator+fatalities that would match key2.
	insertEvent(t, db, "2019-03-10", "ET-AVJ", "Ethiopian", intp(157))
	// Candidate has a DIFFERENT registration but the SAME operator+fatalities.
	cand := ExtractedEvent{Date: "2019-03-10", DatePrecision: "exact",
		AircraftRegistration: "N99999", OperatorName: "Ethiopian", Fatalities: intp(157)}
	_, linked, needsReview, err := FindDuplicateEvent(ctx, db, cand)
	if err != nil || linked || needsReview {
		t.Fatalf("reg present must use key1 only (no key2 fallthrough): linked=%v needsReview=%v err=%v", linked, needsReview, err)
	}
}

func TestFindDuplicateEventNonExactPrecisionSkips(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	insertEvent(t, db, "2019-03-10", "ET-AVJ", "Ethiopian", intp(157))
	cand := ExtractedEvent{Date: "2019-03-10", DatePrecision: "month", AircraftRegistration: "ET-AVJ"}
	_, linked, needsReview, err := FindDuplicateEvent(ctx, db, cand)
	if err != nil || linked || needsReview {
		t.Fatalf("non-exact precision must skip dedup: linked=%v needsReview=%v err=%v", linked, needsReview, err)
	}
}

// TestFindDuplicateEventPlaceholderRegFallsToKey2 is GO-CP-5b's core guard:
// a placeholder registration (Cyrillic "б/н", "N/A", ...) must NOT be used for
// key-1 matching — it must fall through to key-2 like a truly blank
// registration, otherwise every placeholder-registration record in a body-wide
// listing collapses onto the first one seen.
func TestFindDuplicateEventPlaceholderRegFallsToKey2(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	// Two DIFFERENT events, both carrying the Cyrillic placeholder registration,
	// distinguished only by operator/fatalities/aircraft_type.
	insertEventFull(t, db, "2021-07-04", "б/н", "AeroX", intp(2), "An-2", "Omsk")
	want := insertEventFull(t, db, "2021-07-04", "Б/Н", "SibAir", intp(5), "Mi-8", "Tomsk")

	cand := ExtractedEvent{Date: "2021-07-04", DatePrecision: "exact",
		AircraftRegistration: "б/н", OperatorName: "SibAir", Fatalities: intp(5), AircraftType: "Mi-8"}
	id, linked, needsReview, err := FindDuplicateEvent(ctx, db, cand)
	if err != nil || !linked || needsReview || id != want {
		t.Fatalf("placeholder reg key1: id=%d linked=%v needsReview=%v err=%v want %d (SibAir/Mi-8 event, not the first б/н row)",
			id, linked, needsReview, err, want)
	}
}

// TestIsPlaceholderReg pins the blacklist directly.
func TestIsPlaceholderReg(t *testing.T) {
	for _, s := range []string{"", "N/A", "n/a", "unknown", "UNK", "none", "-", "б/н", "Б/Н"} {
		if !isPlaceholderReg(normalizeReg(s)) {
			t.Errorf("isPlaceholderReg(normalizeReg(%q)) = false, want true", s)
		}
	}
	for _, s := range []string{"ET-AVJ", "9M-MRO", "N12345"} {
		if isPlaceholderReg(normalizeReg(s)) {
			t.Errorf("isPlaceholderReg(normalizeReg(%q)) = true, want false", s)
		}
	}
}

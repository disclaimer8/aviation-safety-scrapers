package main

import (
	"database/sql"
	"testing"
)

func newTestDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := InitDB(":memory:")
	if err != nil {
		t.Fatalf("InitDB failed: %v", err)
	}
	t.Cleanup(func() { db.Close() })
	return db
}

func countRows(t *testing.T, db *sql.DB) int {
	t.Helper()
	var n int
	if err := db.QueryRow(`SELECT COUNT(*) FROM accidents`).Scan(&n); err != nil {
		t.Fatalf("count query failed: %v", err)
	}
	return n
}

func getSourceURL(t *testing.T, db *sql.DB, id int) string {
	t.Helper()
	var url string
	if err := db.QueryRow(`SELECT source_url FROM accidents WHERE id = ?`, id).Scan(&url); err != nil {
		t.Fatalf("failed to read source_url for id %d: %v", id, err)
	}
	return url
}

// Exact source-URL dedup: re-inserting the identical accident (same stable
// source URL) must not create a second row.
func TestInsertAccident_ExactSourceURLDedup(t *testing.T) {
	db := newTestDB(t)

	a := Accident{
		Date:          "1 Jan 1985",
		AircraftModel: "Boeing 737",
		Operator:      "Example Air",
		SourceURL:     "http://www.wikidata.org/entity/Q12345",
	}

	if err := InsertAccident(db, a); err != nil {
		t.Fatalf("first insert failed: %v", err)
	}
	if err := InsertAccident(db, a); err != nil {
		t.Fatalf("second insert (exact dup) failed: %v", err)
	}

	if got := countRows(t, db); got != 1 {
		t.Fatalf("expected 1 row after exact-duplicate insert, got %d", got)
	}
}

// Exact source-URL matching must key on whole comma-separated segments, not
// raw substrings, so a URL that is merely a prefix of another must not be
// treated as identical.
func TestInsertAccident_SourceURLSegmentNotSubstring(t *testing.T) {
	db := newTestDB(t)

	a1 := Accident{
		Date:          "1 Jan 1985",
		AircraftModel: "Zephyr Glider",
		Operator:      "Zeta Air",
		SourceURL:     "http://www.wikidata.org/entity/Q1",
	}
	a2 := Accident{
		Date:          "1 Jan 1985",
		AircraftModel: "Zephyr Glider",
		Operator:      "Zeta Air",
		SourceURL:     "http://www.wikidata.org/entity/Q12", // Q1 is a prefix of Q12
	}

	if err := InsertAccident(db, a1); err != nil {
		t.Fatalf("insert a1 failed: %v", err)
	}
	if err := InsertAccident(db, a2); err != nil {
		t.Fatalf("insert a2 failed: %v", err)
	}

	// a1 and a2 share model+operator, but the date is a Jan-1 placeholder so
	// fuzzy matching must be skipped, and the source URLs are NOT an exact
	// segment match (Q1 is only a substring-prefix of Q12), so no merge
	// should happen via either path: expect 2 distinct rows.
	if got := countRows(t, db); got != 2 {
		t.Fatalf("expected 2 distinct rows (no substring false-positive, no Jan-1 fuzzy merge), got %d", got)
	}
}

// Tightened fuzzy match: sharing only ONE of {model-first-word,
// operator-first-word} must no longer merge (previously an OR).
func TestInsertAccident_FuzzyRequiresBothModelAndOperator(t *testing.T) {
	db := newTestDB(t)

	a1 := Accident{
		Date:          "15 Mar 1990",
		AircraftModel: "Cessna 172",
		Operator:      "Acme Airlines",
		SourceURL:     "http://www.wikidata.org/entity/Q100",
	}
	// Same day, same model first word ("Cessna"), but DIFFERENT operator.
	a2 := Accident{
		Date:          "15 Mar 1990",
		AircraftModel: "Cessna 210",
		Operator:      "Unrelated Charter",
		SourceURL:     "http://www.wikidata.org/entity/Q200",
	}

	if err := InsertAccident(db, a1); err != nil {
		t.Fatalf("insert a1 failed: %v", err)
	}
	if err := InsertAccident(db, a2); err != nil {
		t.Fatalf("insert a2 failed: %v", err)
	}

	if got := countRows(t, db); got != 2 {
		t.Fatalf("expected 2 rows since only model matched (not operator too), got %d", got)
	}
}

// Fuzzy match still works (and preserves URL-append merge behavior) when
// BOTH model and operator first words match on a real (non-placeholder) date.
func TestInsertAccident_FuzzyMergesWhenBothMatch(t *testing.T) {
	db := newTestDB(t)

	a1 := Accident{
		Date:          "15 Mar 1990",
		AircraftModel: "Cessna 172",
		Operator:      "Acme Airlines Inc",
		SourceURL:     "http://www.wikidata.org/entity/Q300",
	}
	a2 := Accident{
		Date:          "15 Mar 1990",
		AircraftModel: "Cessna 172N", // shares first word "Cessna"
		Operator:      "Acme Airlines",  // shares first word "Acme"
		SourceURL:     "http://www.wikidata.org/entity/Q301",
	}

	if err := InsertAccident(db, a1); err != nil {
		t.Fatalf("insert a1 failed: %v", err)
	}
	if err := InsertAccident(db, a2); err != nil {
		t.Fatalf("insert a2 failed: %v", err)
	}

	if got := countRows(t, db); got != 1 {
		t.Fatalf("expected 1 merged row when both model and operator match, got %d", got)
	}

	url := getSourceURL(t, db, 1)
	if url != "http://www.wikidata.org/entity/Q300,http://www.wikidata.org/entity/Q301" {
		t.Fatalf("expected merged/appended source_url, got %q", url)
	}
}

// Bare Jan-1 placeholder dates must never be used as a fuzzy-match key, even
// when both model and operator first words match, to prevent the historical
// "collapse an entire year per manufacturer" bug.
func TestInsertAccident_Jan1PlaceholderNeverFuzzyMatches(t *testing.T) {
	db := newTestDB(t)

	a1 := Accident{
		Date:          "1 Jan 1962",
		AircraftModel: "Piper Cub",
		Operator:      "Piper Aviation",
		SourceURL:     "http://www.wikidata.org/entity/Q400",
	}
	a2 := Accident{
		Date:          "1 Jan 1962",
		AircraftModel: "Piper Comanche", // shares first word "Piper"
		Operator:      "Piper Flying Club", // shares first word "Piper"
		SourceURL:     "http://www.wikidata.org/entity/Q401",
	}

	if err := InsertAccident(db, a1); err != nil {
		t.Fatalf("insert a1 failed: %v", err)
	}
	if err := InsertAccident(db, a2); err != nil {
		t.Fatalf("insert a2 failed: %v", err)
	}

	if got := countRows(t, db); got != 2 {
		t.Fatalf("expected 2 distinct rows for Jan-1 placeholder dates (no fuzzy collapse), got %d", got)
	}
}

// Different days never fuzzy match, sanity check unaffected by the change.
func TestInsertAccident_DifferentDayNoMerge(t *testing.T) {
	db := newTestDB(t)

	a1 := Accident{
		Date:          "15 Mar 1990",
		AircraftModel: "Cessna 172",
		Operator:      "Acme Airlines",
		SourceURL:     "http://www.wikidata.org/entity/Q500",
	}
	a2 := Accident{
		Date:          "16 Mar 1990",
		AircraftModel: "Cessna 172",
		Operator:      "Acme Airlines",
		SourceURL:     "http://www.wikidata.org/entity/Q501",
	}

	if err := InsertAccident(db, a1); err != nil {
		t.Fatalf("insert a1 failed: %v", err)
	}
	if err := InsertAccident(db, a2); err != nil {
		t.Fatalf("insert a2 failed: %v", err)
	}

	if got := countRows(t, db); got != 2 {
		t.Fatalf("expected 2 rows for different days, got %d", got)
	}
}

func TestIsJan1Placeholder(t *testing.T) {
	cases := map[string]bool{
		"1962-01-01": true,
		"1962-01-02": false,
		"1962-12-01": false,
		"":           false,
		"not-a-date": false,
	}
	for in, want := range cases {
		if got := isJan1Placeholder(in); got != want {
			t.Errorf("isJan1Placeholder(%q) = %v, want %v", in, got, want)
		}
	}
}

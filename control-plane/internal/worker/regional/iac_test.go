package regional

import (
	"os"
	"testing"
)

func TestParseIAC(t *testing.T) {
	raw, err := os.ReadFile("testdata/iac_listing.html")
	if err != nil {
		t.Fatal(err)
	}
	recs, _, err := parseIAC(raw)
	if err != nil {
		t.Fatalf("parseIAC: %v", err)
	}
	if len(recs) == 0 {
		t.Fatal("expected records from the IAC fixture, got 0")
	}
	for _, r := range recs {
		if r.Ref == "" || r.OriginalURL == "" || r.Title == "" {
			t.Errorf("record missing required field: %+v", r)
		}
	}
	// External + nav links must be excluded; the three report entries kept.
	if len(recs) != 3 {
		t.Fatalf("kept %d records, want 3: %+v", len(recs), recs)
	}
	// Relative hrefs absolute-ified against the mak.aero origin.
	for _, r := range recs {
		if got := r.OriginalURL[:8]; got != "https://" {
			t.Errorf("OriginalURL not absolute: %q", r.OriginalURL)
		}
	}
	// Date parsed from a dd.mm.yyyy title into ISO.
	var found bool
	for _, r := range recs {
		if r.Ref == "2024-ra-0012" {
			found = true
			if r.OccurrenceDate != "2024-01-02" {
				t.Errorf("date = %q, want 2024-01-02", r.OccurrenceDate)
			}
		}
	}
	if !found {
		t.Error("expected record 2024-ra-0012")
	}
}

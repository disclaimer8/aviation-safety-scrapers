package regional

import (
	"os"
	"testing"
)

func TestParseBAGAIA(t *testing.T) {
	raw, err := os.ReadFile("testdata/bagaia_listing.html")
	if err != nil {
		t.Fatal(err)
	}
	recs, _, err := parseBAGAIA(raw)
	if err != nil {
		t.Fatalf("parseBAGAIA: %v", err)
	}
	if len(recs) != 3 {
		t.Fatalf("kept %d records, want 3: %+v", len(recs), recs)
	}
	for _, r := range recs {
		if r.Ref == "" || r.OriginalURL == "" || r.Title == "" {
			t.Errorf("record missing required field: %+v", r)
		}
		if r.OriginalURL[:8] != "https://" {
			t.Errorf("OriginalURL not absolute: %q", r.OriginalURL)
		}
	}
	for _, r := range recs {
		if r.Ref == "bagaia-2024-ng-003-final" && r.OccurrenceDate != "2024-02-19" {
			t.Errorf("date = %q, want 2024-02-19", r.OccurrenceDate)
		}
	}
}

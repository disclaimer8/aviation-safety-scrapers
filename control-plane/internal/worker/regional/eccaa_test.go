package regional

import (
	"os"
	"testing"
)

func TestParseECCAA(t *testing.T) {
	raw, err := os.ReadFile("testdata/eccaa_listing.html")
	if err != nil {
		t.Fatal(err)
	}
	recs, _, err := parseECCAA(raw)
	if err != nil {
		t.Fatalf("parseECCAA: %v", err)
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
	// The PDF entry carries report metadata.
	for _, r := range recs {
		if r.Ref == "2022-004-dm-final" {
			if r.Mimetype != "application/pdf" || r.ReportURL == "" {
				t.Errorf("PDF entry missing report metadata: %+v", r)
			}
			if r.OccurrenceDate != "2022-05-30" {
				t.Errorf("date = %q, want 2022-05-30", r.OccurrenceDate)
			}
		}
	}
}

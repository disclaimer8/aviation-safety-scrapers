package regional

import (
	"os"
	"strings"
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
	// The real mak-iac.org/rassledovaniya/ snapshot holds many dated reports.
	if len(recs) < 10 {
		t.Fatalf("expected the dated IAC reports, got %d", len(recs))
	}
	for _, r := range recs {
		if r.Ref == "" || r.OriginalURL == "" || r.Title == "" {
			t.Errorf("record missing required field: %+v", r)
		}
		if !strings.HasPrefix(r.OriginalURL, "https://mak-iac.org/rassledovaniya/") {
			t.Errorf("OriginalURL not an absolute mak-iac.org report URL: %q", r.OriginalURL)
		}
		// Navigation pages under /rassledovaniya/ must be excluded.
		if r.Ref == "o-komissii" || r.Ref == "bezopasnost-poletov" || r.Ref == "tekhnicheskaya-laboratoriya" {
			t.Errorf("navigation page leaked as a report: %q", r.Ref)
		}
	}
	// A known report carries its date, parsed from the dd.mm.yyyy link text.
	var found bool
	for _, r := range recs {
		if r.Ref == "an-2-ra-40440-19-05-2026" {
			found = true
			if r.OccurrenceDate != "2026-05-19" {
				t.Errorf("date = %q, want 2026-05-19", r.OccurrenceDate)
			}
		}
	}
	if !found {
		t.Error("expected report an-2-ra-40440-19-05-2026")
	}
}

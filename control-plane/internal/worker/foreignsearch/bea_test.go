package foreignsearch

import (
	"os"
	"testing"
)

func TestParseBEA(t *testing.T) {
	raw, err := os.ReadFile("testdata/bea_listing.html")
	if err != nil {
		t.Fatal(err)
	}
	recs, _, err := parseBEA(raw)
	if err != nil {
		t.Fatalf("parseBEA: %v", err)
	}
	if len(recs) == 0 {
		t.Fatal("expected records from the captured BEA listing, got 0")
	}
	for _, r := range recs {
		if r.ForeignRef == "" || r.OriginalURL == "" || r.Title == "" {
			t.Errorf("record missing required field: %+v", r)
		}
		// OriginalURL must be absolute.
		if len(r.OriginalURL) < 8 || r.OriginalURL[:8] != "https://" {
			t.Errorf("OriginalURL is not absolute: %q", r.OriginalURL)
		}
		// OccurrenceDate must be yyyy-mm-dd when non-empty.
		if d := r.OccurrenceDate; d != "" {
			if len(d) != 10 || d[4] != '-' || d[7] != '-' {
				t.Errorf("OccurrenceDate %q is not yyyy-mm-dd for ref %s", d, r.ForeignRef)
			}
		}
	}
}

func TestParseBEAEmptyBody(t *testing.T) {
	recs, warnings, err := parseBEA([]byte("<html><body>nothing here</body></html>"))
	if err != nil {
		t.Fatalf("parseBEA on empty body returned error: %v", err)
	}
	if len(recs) != 0 {
		t.Errorf("expected 0 records on empty body, got %d", len(recs))
	}
	_ = warnings // zero or more warnings is fine
}

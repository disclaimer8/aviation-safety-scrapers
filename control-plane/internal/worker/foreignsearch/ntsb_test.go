package foreignsearch

import (
	"os"
	"testing"
)

func TestParseNTSB(t *testing.T) {
	raw, err := os.ReadFile("testdata/ntsb_carol.json")
	if err != nil {
		t.Fatal(err)
	}
	recs, warnings, err := parseNTSB(raw)
	if err != nil {
		t.Fatalf("parseNTSB: %v", err)
	}
	if len(recs) == 0 {
		t.Fatalf("expected records from the captured CAROL fixture, got 0 (warnings=%d)", warnings)
	}
	for _, r := range recs {
		if r.ForeignRef == "" {
			t.Errorf("record missing ForeignRef: %+v", r)
		}
		if r.OriginalURL == "" {
			t.Errorf("record missing OriginalURL: %+v", r)
		}
		if r.Title == "" {
			t.Errorf("record missing Title: %+v", r)
		}
		// OccurrenceDate must be yyyy-mm-dd when non-empty.
		if d := r.OccurrenceDate; d != "" {
			if len(d) != 10 || d[4] != '-' || d[7] != '-' {
				t.Errorf("OccurrenceDate %q is not yyyy-mm-dd for ref %s", d, r.ForeignRef)
			}
		}
	}
}

func TestParseNTSBErrorsOnGarbage(t *testing.T) {
	if _, _, err := parseNTSB([]byte("not json")); err == nil {
		t.Fatal("expected error on unparseable body")
	}
}

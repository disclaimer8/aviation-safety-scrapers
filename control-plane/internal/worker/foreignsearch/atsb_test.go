package foreignsearch

import (
	"context"
	"os"
	"testing"
)

func TestParseATSB(t *testing.T) {
	raw, err := os.ReadFile("testdata/atsb_export.json")
	if err != nil {
		t.Fatal(err)
	}
	recs, _, err := parseATSB(raw)
	if err != nil {
		t.Fatalf("parseATSB: %v", err)
	}
	if len(recs) == 0 {
		t.Fatal("expected records from the ATSB export fixture, got 0")
	}
	for _, r := range recs {
		if r.ForeignRef == "" || r.OriginalURL == "" || r.Title == "" {
			t.Errorf("record missing required field: %+v", r)
		}
	}
}

func TestATSBClientReadsSourceFile(t *testing.T) {
	c := NewATSBClient("testdata/atsb_export.json")
	recs, err := c.Search(context.Background(), "WS")
	if err != nil {
		t.Fatal(err)
	}
	if len(recs) == 0 {
		t.Fatal("ATSB client should parse the source file into records")
	}
}

package manufacturer

import (
	"os"
	"strings"
	"testing"
)

func TestParseSafetyFirstListing(t *testing.T) {
	html, err := os.ReadFile("testdata/safetyfirst_listing.html")
	if err != nil {
		t.Fatal(err)
	}
	recs, err := ParseSafetyFirstListing(html, "https://safetyfirst.airbus.com/magazine/")
	if err != nil {
		t.Fatal(err)
	}
	if len(recs) == 0 {
		t.Fatal("no records parsed from fixture")
	}

	// Validate every record has the required fields and absolute http(s) URLs.
	for _, r := range recs {
		if r.IssueRef == "" || r.Title == "" || r.OriginalURL == "" {
			t.Errorf("incomplete record: %+v", r)
		}
		if !(len(r.OriginalURL) >= 8 && (r.OriginalURL[:7] == "http://" || r.OriginalURL[:8] == "https://")) {
			t.Errorf("non-absolute/non-http OriginalURL: %s", r.OriginalURL)
		}
		if r.ReportURL != "" {
			if !(len(r.ReportURL) >= 8 && (r.ReportURL[:7] == "http://" || r.ReportURL[:8] == "https://")) {
				t.Errorf("non-absolute/non-http ReportURL: %s", r.ReportURL)
			}
		}
	}

	// Pin the fixture exactly: the snapshot holds 43 entries (issues 1–41 + 2
	// special editions). The fixture is frozen, so the parser must yield exactly
	// 43 — fewer means a dropped/over-eager regex, more means a dedup regression.
	// Update this when the testdata snapshot is refreshed.
	if len(recs) != 43 {
		t.Errorf("expected exactly 43 records, got %d", len(recs))
	}

	// Pin issue #41 (highest numbered issue as of 2026-06-23).
	var found41 *ManufacturerRecord
	for i := range recs {
		if recs[i].IssueRef == "41" {
			found41 = &recs[i]
			break
		}
	}
	if found41 == nil {
		t.Fatal("record with IssueRef=41 not found")
	}
	if !strings.Contains(found41.Title, "41") {
		t.Errorf("issue 41 title should reference 41, got: %q", found41.Title)
	}
	if !strings.HasSuffix(found41.ReportURL, "safety_first_41.pdf") {
		t.Errorf("issue 41 ReportURL should end with safety_first_41.pdf, got: %q", found41.ReportURL)
	}
	if found41.OriginalURL != found41.ReportURL {
		t.Errorf("for a direct-PDF listing, OriginalURL should equal ReportURL; got OriginalURL=%q ReportURL=%q",
			found41.OriginalURL, found41.ReportURL)
	}

	// Pin a special edition (control your speed).
	var foundSpeed *ManufacturerRecord
	for i := range recs {
		if strings.Contains(strings.ToLower(recs[i].IssueRef), "speed") ||
			strings.Contains(strings.ToLower(recs[i].Title), "speed") {
			foundSpeed = &recs[i]
			break
		}
	}
	if foundSpeed == nil {
		t.Fatal("special edition 'control your speed' not found")
	}
	if !strings.HasSuffix(foundSpeed.ReportURL, ".pdf") {
		t.Errorf("special edition ReportURL should be a PDF, got: %q", foundSpeed.ReportURL)
	}
}

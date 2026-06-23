package wayback

import (
	"os"
	"testing"
)

func TestParseCDXFiltersAndCollapses(t *testing.T) {
	raw, err := os.ReadFile("testdata/cdx_sample.json")
	if err != nil {
		t.Fatal(err)
	}
	snaps, warnings, err := ParseCDX(raw)
	if err != nil {
		t.Fatal(err)
	}
	if warnings != 0 {
		t.Errorf("warnings = %d, want 0", warnings)
	}
	// DIGESTA collapsed to one, DIGESTB kept, DIGESTC (html) dropped, DIGESTD (404) dropped.
	if len(snaps) != 2 {
		t.Fatalf("len(snaps) = %d, want 2 (%+v)", len(snaps), snaps)
	}
	byDigest := map[string]Snapshot{}
	for _, s := range snaps {
		byDigest[s.Digest] = s
	}
	a, ok := byDigest["DIGESTA"]
	if !ok {
		t.Fatal("DIGESTA missing")
	}
	if a.ArchivedURL != "https://web.archive.org/web/20100101000000id_/http://example.gov/a.pdf" {
		t.Errorf("ArchivedURL = %q", a.ArchivedURL)
	}
	if a.Length != 1024 {
		t.Errorf("Length = %d, want 1024", a.Length)
	}
}

func TestParseCDXCountsMalformedAsWarnings(t *testing.T) {
	raw, err := os.ReadFile("testdata/cdx_malformed.json")
	if err != nil {
		t.Fatal(err)
	}
	snaps, warnings, err := ParseCDX(raw)
	if err != nil {
		t.Fatal(err)
	}
	// One short row (skipped) + one bad length (skipped) = 2 warnings, 0 snapshots.
	if warnings != 2 {
		t.Errorf("warnings = %d, want 2", warnings)
	}
	if len(snaps) != 0 {
		t.Errorf("len(snaps) = %d, want 0", len(snaps))
	}
}

func TestParseCDXErrorsOnGarbage(t *testing.T) {
	if _, _, err := ParseCDX([]byte("not json")); err == nil {
		t.Fatal("expected error on unparseable body")
	}
}

package aia

import (
	"os"
	"strings"
	"testing"
)

// openFixture opens the offline ICAO AIA fixture for the parser tests.
func openFixture(t *testing.T) *os.File {
	t.Helper()
	f, err := os.Open("../../../fixtures/icao/aia.html")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = f.Close() })
	return f
}

func TestParseAIAFixture(t *testing.T) {
	records, err := Parse(openFixture(t))
	if err != nil {
		t.Fatal(err)
	}
	byCountry := map[string]Record{}
	for _, r := range records {
		byCountry[r.CountryLabel] = r
	}

	if got := byCountry["Angola"]; got.WebsiteURL != "https://initpat.gov.ao" ||
		len(got.Emails) < 2 || got.AuthorityName == "" {
		t.Fatalf("Angola=%+v", got)
	}
	if got := byCountry["Anguilla"]; got.ReferenceCountry != "United Kingdom" {
		t.Fatalf("Anguilla=%+v", got)
	}
	if got := byCountry["Antigua and Barbuda"]; got.ReferenceBody != "Eastern Caribbean States" {
		t.Fatalf("Antigua=%+v", got)
	}
}

// TestParseDeobfuscatesExplicitEmail verifies that only explicit [at]/[dot]
// obfuscation is decoded, producing a normal address.
func TestParseDeobfuscatesExplicitEmail(t *testing.T) {
	records, err := Parse(openFixture(t))
	if err != nil {
		t.Fatal(err)
	}
	var albania Record
	for _, r := range records {
		if r.CountryLabel == "Albania" {
			albania = r
		}
	}
	want := "aaiiu@infrastruktura.gov.al"
	found := false
	for _, e := range albania.Emails {
		if e == want {
			found = true
		}
	}
	if !found {
		t.Fatalf("Albania emails=%v, want deobfuscated %q", albania.Emails, want)
	}
}

// TestParseMalformedBlockYieldsWarning verifies the malformed Azerbaijan block
// is preserved as a record carrying a warning rather than crashing the parser.
func TestParseMalformedBlockYieldsWarning(t *testing.T) {
	records, err := Parse(openFixture(t))
	if err != nil {
		t.Fatal(err)
	}
	var az Record
	found := false
	for _, r := range records {
		if r.CountryLabel == "Azerbaijan" {
			az = r
			found = true
		}
	}
	if !found {
		t.Fatal("Azerbaijan record missing; malformed blocks must still be staged")
	}
	if len(az.Warnings) == 0 {
		t.Fatalf("Azerbaijan=%+v: expected a warning for the malformed block", az)
	}
	if az.RawContact == "" {
		t.Fatal("Azerbaijan raw contact must be preserved")
	}
}

// TestParseRecordCount sanity-checks that every country heading became a record.
func TestParseRecordCount(t *testing.T) {
	records, err := Parse(openFixture(t))
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 10 {
		labels := make([]string, len(records))
		for i, r := range records {
			labels[i] = r.CountryLabel
		}
		t.Fatalf("want 10 records, got %d: %s", len(records), strings.Join(labels, ", "))
	}
}

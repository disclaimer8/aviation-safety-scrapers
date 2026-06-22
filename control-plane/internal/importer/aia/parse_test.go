package aia

import (
	"os"
	"strings"
	"testing"
)

// openFixture opens the offline, trimmed-but-real ICAO AIA fixture.
func openFixture(t *testing.T) *os.File {
	t.Helper()
	f, err := os.Open("../../../fixtures/icao/aia.html")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = f.Close() })
	return f
}

func parseFixture(t *testing.T) map[string]Record {
	t.Helper()
	records, err := Parse(openFixture(t))
	if err != nil {
		t.Fatal(err)
	}
	byCountry := map[string]Record{}
	for _, r := range records {
		byCountry[r.CountryLabel] = r
	}
	return byCountry
}

// TestParseAfghanistan verifies a plain contracting-state row: authority name is
// the first line and mentions civil aviation, with phone/fax extracted.
func TestParseAfghanistan(t *testing.T) {
	got := parseFixture(t)["Afghanistan"]
	if got.AuthorityName == "" {
		t.Fatalf("Afghanistan: empty authority name (raw=%q)", got.RawContact)
	}
	if !strings.Contains(strings.ToLower(got.AuthorityName+got.RawContact), "civil aviation") {
		t.Fatalf("Afghanistan: expected civil aviation in block, got %+v", got)
	}
	if len(got.Phones) == 0 {
		t.Fatalf("Afghanistan: expected a phone, got %+v", got)
	}
}

// TestParseAlbania verifies the Updated date (D Month YYYY), a Tel, and the
// spamspan email is deobfuscated to a single clean address (no garbage from the
// parenthetical [dot] hint span).
func TestParseAlbania(t *testing.T) {
	got := parseFixture(t)["Albania"]
	if len(got.Phones) == 0 {
		t.Fatalf("Albania: expected a Tel, got %+v", got)
	}
	want := "info@akisa.gov.al"
	found := false
	for _, e := range got.Emails {
		if e == want {
			found = true
		}
		if strings.Contains(e, "[dot]") || strings.Contains(e, "[at]") {
			t.Fatalf("Albania: leaked obfuscated email %q", e)
		}
	}
	if !found {
		t.Fatalf("Albania emails=%v, want %q", got.Emails, want)
	}
	if got.UpdatedAt == nil || got.UpdatedAt.Format("2006-01-02") != "2023-08-01" {
		t.Fatalf("Albania UpdatedAt=%v, want 2023-08-01", got.UpdatedAt)
	}
}

// TestParseAngola verifies a row with a Website label and multiple emails.
func TestParseAngola(t *testing.T) {
	got := parseFixture(t)["Angola"]
	if got.WebsiteURL != "https://initpat.gov.ao" {
		t.Fatalf("Angola WebsiteURL=%q", got.WebsiteURL)
	}
	if len(got.Emails) < 2 {
		t.Fatalf("Angola emails=%v, want >=2", got.Emails)
	}
	if got.AuthorityName == "" {
		t.Fatalf("Angola: empty authority name")
	}
}

// TestParseDependentTerritoryRefersToCountry verifies the Anguilla (DT) row
// captures ReferenceCountry and is not treated as a canonical authority.
func TestParseDependentTerritoryRefersToCountry(t *testing.T) {
	got := parseFixture(t)["Anguilla (DT)"]
	if got.ReferenceCountry != "United Kingdom" {
		t.Fatalf("Anguilla (DT) ReferenceCountry=%q, want United Kingdom (%+v)", got.ReferenceCountry, got)
	}
	if got.AuthorityName != "" {
		t.Fatalf("Anguilla (DT): delegation must not yield an authority, got %q", got.AuthorityName)
	}
}

// TestParseSeeRegionalBody verifies the "See Eastern Caribbean States" rows
// capture ReferenceBody, covering both a contracting state and an NCS row.
func TestParseSeeRegionalBody(t *testing.T) {
	by := parseFixture(t)
	for _, label := range []string{"Antigua and Barbuda", "Dominica (NCS)"} {
		got := by[label]
		if got.ReferenceBody != "Eastern Caribbean States" {
			t.Fatalf("%s ReferenceBody=%q, want Eastern Caribbean States (%+v)", label, got.ReferenceBody, got)
		}
	}
}

// TestParseMessyRowPreserved verifies the messy data-teams/mailto Sierra Leone
// row still parses an authority, phone, and email without crashing.
func TestParseMessyRow(t *testing.T) {
	got := parseFixture(t)["Sierra Leone"]
	if got.AuthorityName == "" || got.RawContact == "" {
		t.Fatalf("Sierra Leone: lost data, got %+v", got)
	}
	if len(got.Emails) == 0 {
		t.Fatalf("Sierra Leone: expected an email, got %+v", got)
	}
}

// TestParseRecordCount checks every data row in the fixture became a record.
func TestParseRecordCount(t *testing.T) {
	records, err := Parse(openFixture(t))
	if err != nil {
		t.Fatal(err)
	}
	const want = 9
	if len(records) != want {
		labels := make([]string, len(records))
		for i, r := range records {
			labels[i] = r.CountryLabel
		}
		t.Fatalf("want %d records, got %d: %s", want, len(records), strings.Join(labels, ", "))
	}
}

// TestParseFullRealPage parses the captured 444 KB live page when present,
// asserting a high record count. Skipped in CI where the file is absent.
func TestParseFullRealPage(t *testing.T) {
	path := "../../../../.realpages/icao_aia_full.html"
	if _, err := os.Stat(path); err != nil {
		t.Skipf("real page not present: %v", err)
	}
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	records, err := Parse(f)
	if err != nil {
		t.Fatal(err)
	}
	if len(records) < 150 {
		t.Fatalf("full real page: parsed %d records, want >= 150", len(records))
	}
}

package raio

import (
	"os"
	"testing"
)

func loadFixtureRecords(t *testing.T) map[string]BodyRecord {
	t.Helper()
	f, err := os.Open("../../../fixtures/icao/raio.html")
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	records, err := Parse(f)
	if err != nil {
		t.Fatal(err)
	}
	byCode := map[string]BodyRecord{}
	for _, r := range records {
		byCode[r.Code] = r
	}
	return byCode
}

func contains(ss []string, want string) bool {
	for _, s := range ss {
		if s == want {
			return true
		}
	}
	return false
}

// TestParseRAIOAndICMSections verifies the class is derived from which of the two
// real tables a body sits in (first = RAIO, second = ICM).
func TestParseRAIOAndICMSections(t *testing.T) {
	byCode := loadFixtureRecords(t)

	if byCode["BAGAIA"].Class != "raio" || len(byCode["BAGAIA"].Members) != 7 {
		t.Fatalf("BAGAIA=%+v", byCode["BAGAIA"])
	}
	if byCode["IAC"].Class != "raio" || len(byCode["IAC"].Members) != 8 {
		t.Fatalf("IAC=%+v", byCode["IAC"])
	}
	if byCode["ARCM-MENA"].Class != "icm" {
		t.Fatalf("ARCM-MENA=%+v", byCode["ARCM-MENA"])
	}
	if byCode["ENCASIA"].Class != "icm" {
		t.Fatalf("ENCASIA=%+v", byCode["ENCASIA"])
	}
	if len(byCode["ARCM-SAM"].Observers) == 0 {
		t.Fatal("expected ARCM-SAM observers")
	}
}

// TestParseSplitsMixedCommaSemicolon verifies BAGAIA's comma-separated and IAC's
// mixed comma/semicolon member lists each yield exactly the right distinct labels.
func TestParseSplitsMixedCommaSemicolon(t *testing.T) {
	byCode := loadFixtureRecords(t)
	want := []string{"Cabo Verde", "Gambia", "Ghana", "Guinea", "Liberia", "Nigeria", "Sierra Leone"}
	got := byCode["BAGAIA"].Members
	if len(got) != len(want) {
		t.Fatalf("BAGAIA members=%v", got)
	}
	for i, w := range want {
		if got[i] != w {
			t.Fatalf("BAGAIA member %d=%q want %q (all=%v)", i, got[i], w, got)
		}
	}

	// IAC mixes both separators: "Armenia; Azerbaijan; Belarus; Kazakhstan;
	// Kyrgyzstan, Tajikistan, Turkmenistan, Russian Federation".
	iac := byCode["IAC"].Members
	if len(iac) != 8 {
		t.Fatalf("IAC members=%v want 8", iac)
	}
	if !contains(iac, "Armenia") || !contains(iac, "Russian Federation") || !contains(iac, "Kyrgyzstan") {
		t.Fatalf("IAC members=%v missing an expected label", iac)
	}
}

// TestParseObserversExcludedFromMembers verifies the two real observer clauses are
// split out of Members: ARCM-SAM "SPECIAL OBSERVERS:" and ENCASIA "+ Observers:".
func TestParseObserversExcludedFromMembers(t *testing.T) {
	byCode := loadFixtureRecords(t)

	sam := byCode["ARCM-SAM"]
	// Real ARCM-SAM observers: Dominican Republic, Cuba, BEA, NTSB, CASSOS.
	if !contains(sam.Observers, "Dominican Republic") || !contains(sam.Observers, "Cuba") {
		t.Fatalf("ARCM-SAM observers=%v want Dominican Republic and Cuba", sam.Observers)
	}
	// Members must not contain the observer-clause labels.
	for _, bad := range []string{"Dominican Republic", "Cuba", "BEA", "NTSB"} {
		if contains(sam.Members, bad) {
			t.Fatalf("observer %q leaked into ARCM-SAM members: %v", bad, sam.Members)
		}
	}
	// Members still carry the real participants (12: ... and Venezuela).
	if !contains(sam.Members, "Argentina") || !contains(sam.Members, "Venezuela") {
		t.Fatalf("ARCM-SAM members=%v missing real participants", sam.Members)
	}

	enc := byCode["ENCASIA"]
	// Real ENCASIA observers: Iceland, Norway, Kososvo (UNSCR ...), EASA.
	if !contains(enc.Observers, "Iceland") || !contains(enc.Observers, "Norway") {
		t.Fatalf("ENCASIA observers=%v want Iceland and Norway", enc.Observers)
	}
	for _, bad := range []string{"Iceland", "Norway", "EASA"} {
		if contains(enc.Members, bad) {
			t.Fatalf("observer %q leaked into ENCASIA members: %v", bad, enc.Members)
		}
	}
	// A real EU member must remain in Members (e.g. Spain, the last before "+ Observers:").
	if !contains(enc.Members, "Spain") || !contains(enc.Members, "Austria") {
		t.Fatalf("ENCASIA members=%v missing EU participants", enc.Members)
	}
}

// TestParseWebsite verifies the website href is captured from the cell's anchor.
func TestParseWebsite(t *testing.T) {
	byCode := loadFixtureRecords(t)
	if got := byCode["BAGAIA"].WebsiteURL; got != "https://www.bagaia.org/" {
		t.Fatalf("BAGAIA WebsiteURL=%q", got)
	}
}

// TestParseFullRealPage parses the captured live page when present, asserting the
// known bodies, member counts, and that observers were separated. Skipped in CI
// where the file is absent.
func TestParseFullRealPage(t *testing.T) {
	path := "../../../../.realpages/icao_raio_full.html"
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
	byCode := map[string]BodyRecord{}
	for _, r := range records {
		byCode[r.Code] = r
	}
	if byCode["BAGAIA"].Class != "raio" || len(byCode["BAGAIA"].Members) != 7 {
		t.Fatalf("full page BAGAIA=%+v", byCode["BAGAIA"])
	}
	if byCode["IAC"].Class != "raio" || len(byCode["IAC"].Members) != 8 {
		t.Fatalf("full page IAC=%+v", byCode["IAC"])
	}
	if byCode["ARCM-MENA"].Class != "icm" {
		t.Fatalf("full page ARCM-MENA=%+v", byCode["ARCM-MENA"])
	}
	if len(byCode["ARCM-SAM"].Observers) == 0 || len(byCode["ENCASIA"].Observers) == 0 {
		t.Fatalf("full page: observers not separated SAM=%v ENCASIA=%v",
			byCode["ARCM-SAM"].Observers, byCode["ENCASIA"].Observers)
	}
	if _, ok := byCode["GRIAA"]; !ok {
		t.Fatalf("full page: GRIAA missing (codes=%v)", byCode)
	}
}

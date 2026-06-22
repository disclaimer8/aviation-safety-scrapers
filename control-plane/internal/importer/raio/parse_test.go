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
	if len(byCode["ARCM-SAM"].Observers) == 0 {
		t.Fatal("expected ARCM-SAM observers")
	}
}

// TestParseSplitsMixedCommaSemicolon verifies BAGAIA's mixed comma/semicolon
// separated member list yields exactly the seven distinct labels.
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
}

// TestParseObserversExcludedFromMembers verifies observer clauses are stripped
// from the member list and captured separately.
func TestParseObserversExcludedFromMembers(t *testing.T) {
	byCode := loadFixtureRecords(t)
	sam := byCode["ARCM-SAM"]
	for _, m := range sam.Members {
		if m == "Panama" || m == "Mexico" {
			t.Fatalf("observer leaked into members: %v", sam.Members)
		}
	}
	hasPanama, hasMexico := false, false
	for _, o := range sam.Observers {
		switch o {
		case "Panama":
			hasPanama = true
		case "Mexico":
			hasMexico = true
		}
	}
	if !hasPanama || !hasMexico {
		t.Fatalf("ARCM-SAM observers=%v want Panama and Mexico", sam.Observers)
	}
	// Members must still carry the four real participants.
	if len(sam.Members) != 4 {
		t.Fatalf("ARCM-SAM members=%v want 4", sam.Members)
	}
}

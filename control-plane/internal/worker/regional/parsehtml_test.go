package regional

import "testing"

func TestParseListingAllowsOnlyHTTPSchemes(t *testing.T) {
	raw := []byte(`
		<a href="javascript:void(0)">JS report 2024</a>
		<a href="data:text/html,report 2024">data report 2024</a>
		<a href="vbscript:msgbox('2024')">vb report 2024</a>
		<a href="/reports/2024-real-01">real report 2024-01-02</a>
	`)
	recs, _, err := parseListing(raw, "https://x.org", func(abs string) bool {
		return hostMatch(abs, "x.org") && looksLikeReport(abs)
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(recs) != 1 || recs[0].Ref != "2024-real-01" {
		t.Fatalf("expected only the http(s) report kept, got %+v", recs)
	}
	for _, r := range recs {
		if r.OriginalURL[:5] != "https" {
			t.Errorf("non-http(s) URL leaked: %q", r.OriginalURL)
		}
	}
}

func TestExtractDateValidatesCalendar(t *testing.T) {
	cases := map[string]string{
		"crash on 2024-01-02":         "2024-01-02",
		"slash form 2024/03/11 here":  "2024-03-11",
		"euro 11.09.2023 form":        "2023-09-11",
		"report no 2023-45-67":        "", // month 45 / day 67 → rejected
		"impossible 2024-13-09":       "", // month 13 → rejected
		"feb 1999/02/30 overflow":     "", // Feb 30 → rejected
		"no date at all in this text": "",
	}
	for in, want := range cases {
		if got := extractDate(in); got != want {
			t.Errorf("extractDate(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestRefFromURLDistinguishesQueryAndExtension(t *testing.T) {
	cases := map[string]string{
		"https://x.org/reports/2024-ra-01":      "2024-ra-01",
		"https://x.org/report.pdf":              "report",            // known ext stripped
		"https://x.org/v1.2-final":              "v1.2-final",        // non-ext dot kept
		"https://x.org/index.php?p=123":         "index?p=123",       // query folded in
		"https://x.org/index.php?p=456":         "index?p=456",       // distinct from p=123
		"https://x.org/?report=789":             "report=789",        // query-only path
	}
	for in, want := range cases {
		if got := refFromURL(in); got != want {
			t.Errorf("refFromURL(%q) = %q, want %q", in, got, want)
		}
	}
	// The two WordPress-style query URLs must yield distinct refs so neither is
	// silently dropped by the seen[ref] dedup.
	if refFromURL("https://x.org/p.php?p=1") == refFromURL("https://x.org/p.php?p=2") {
		t.Fatal("query-distinguished URLs collapsed to one ref")
	}
}

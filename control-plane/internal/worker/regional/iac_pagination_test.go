package regional

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

// iacPageHTML builds a minimal Bitrix-shaped listing page with one anchor per
// ref, each matching parseIAC's filter (hosted on mak-iac.org, under
// /rassledovaniya/, with a 4-digit year in the path).
func iacPageHTML(refs ...string) string {
	var b strings.Builder
	b.WriteString("<html><body>")
	for _, ref := range refs {
		fmt.Fprintf(&b, `<a href="https://mak-iac.org/rassledovaniya/%s-2020/">Report %s</a>`, ref, ref)
	}
	b.WriteString("</body></html>")
	return b.String()
}

// newIACRenderServer returns an httptest render-endpoint server: it decodes
// the {"url":...} POST body, matches the PAGEN_1 query param against pages
// (page 1 = pages[0], no PAGEN_1 param), and serves the corresponding HTML.
// Returns the server plus a thread-safe recorder of every requested URL.
func newIACRenderServer(t *testing.T, pages []string) (*httptest.Server, func() []string) {
	t.Helper()
	var mu sync.Mutex
	var urls []string
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var payload struct {
			URL string `json:"url"`
		}
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		mu.Lock()
		urls = append(urls, payload.URL)
		mu.Unlock()

		page := 1
		if idx := strings.Index(payload.URL, "PAGEN_1="); idx >= 0 {
			fmt.Sscanf(payload.URL[idx+len("PAGEN_1="):], "%d", &page)
		}
		if page < 1 || page > len(pages) {
			w.Write([]byte("<html><body></body></html>")) // no new refs -> stop
			return
		}
		w.Write([]byte(pages[page-1]))
	}))
	t.Cleanup(ts.Close)
	return ts, func() []string {
		mu.Lock()
		defer mu.Unlock()
		out := make([]string, len(urls))
		copy(out, urls)
		return out
	}
}

// TestIACSearchWalksPagesUntilZeroNew is GO-CP-10's core regression guard:
// the previous code only ever fetched page 1. Here page 1 and page 2 each
// carry new refs, and page 3 carries none (a real Bitrix "no more results"
// page) — Search must walk pages 1, 2, and 3 (to discover 3 has nothing new)
// and return the union of refs from pages 1-2, then stop.
func TestIACSearchWalksPagesUntilZeroNew(t *testing.T) {
	page1 := iacPageHTML("an-2-ra-0001", "an-2-ra-0002")
	page2 := iacPageHTML("an-2-ra-0003", "an-2-ra-0004")
	page3 := iacPageHTML() // empty: no new refs, must stop here
	ts, getURLs := newIACRenderServer(t, []string{page1, page2, page3})

	c := &iacClient{renderEndpoint: ts.URL}
	recs, warnings, err := c.Search(context.Background(), "RU")
	if err != nil {
		t.Fatalf("Search: %v", err)
	}
	if warnings != 0 {
		t.Errorf("warnings = %d, want 0", warnings)
	}
	if len(recs) != 4 {
		t.Fatalf("len(recs) = %d, want 4 (2 from page1 + 2 from page2)", len(recs))
	}
	seen := map[string]bool{}
	for _, r := range recs {
		if seen[r.Ref] {
			t.Fatalf("duplicate ref %q across pages", r.Ref)
		}
		seen[r.Ref] = true
	}

	urls := getURLs()
	if len(urls) != 3 {
		t.Fatalf("pages fetched = %d, want 3 (page1, page2, page3-which-had-nothing-new)", len(urls))
	}
	if strings.Contains(urls[0], "PAGEN_1") {
		t.Errorf("first request must be plain page 1 (no PAGEN_1), got %q", urls[0])
	}
	if !strings.Contains(urls[1], "PAGEN_1=2") {
		t.Errorf("second request must be PAGEN_1=2, got %q", urls[1])
	}
	if !strings.Contains(urls[2], "PAGEN_1=3") {
		t.Errorf("third request must be PAGEN_1=3, got %q", urls[2])
	}
}

// TestIACSearchSinglePageStopsImmediately verifies the common case (a small
// listing that fits on page 1) makes exactly one render request — pagination
// must not force extra round trips when page 1 alone has no new refs beyond
// itself... i.e. when page 2 has nothing new, we stop after checking it once.
func TestIACSearchSinglePageStopsImmediately(t *testing.T) {
	page1 := iacPageHTML("an-2-ra-0001")
	ts, getURLs := newIACRenderServer(t, []string{page1, iacPageHTML()})

	c := &iacClient{renderEndpoint: ts.URL}
	recs, _, err := c.Search(context.Background(), "RU")
	if err != nil {
		t.Fatalf("Search: %v", err)
	}
	if len(recs) != 1 {
		t.Fatalf("len(recs) = %d, want 1", len(recs))
	}
	if urls := getURLs(); len(urls) != 2 {
		t.Fatalf("pages fetched = %d, want 2 (page1 + the empty page2 that ends the walk)", len(urls))
	}
}

// TestIACSearchHitsPageCap verifies the iacMaxPages safety cap: a listing
// that always yields a new ref (would otherwise loop forever) must stop at
// iacMaxPages.
func TestIACSearchHitsPageCap(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var payload struct {
			URL string `json:"url"`
		}
		json.NewDecoder(r.Body).Decode(&payload)
		page := 1
		if idx := strings.Index(payload.URL, "PAGEN_1="); idx >= 0 {
			fmt.Sscanf(payload.URL[idx+len("PAGEN_1="):], "%d", &page)
		}
		// Every page contributes exactly one brand-new ref, forever.
		w.Write([]byte(iacPageHTML(fmt.Sprintf("an-2-ra-%04d", page))))
	}))
	defer ts.Close()

	c := &iacClient{renderEndpoint: ts.URL}
	recs, _, err := c.Search(context.Background(), "RU")
	if err != nil {
		t.Fatalf("Search: %v", err)
	}
	if len(recs) != iacMaxPages {
		t.Fatalf("len(recs) = %d, want iacMaxPages=%d (loop must terminate at the cap)", len(recs), iacMaxPages)
	}
}

// TestIACSearchSourceFileIsSinglePage verifies the out-of-band sourceFile
// path is NOT paginated (a local export has no "next page" to fetch).
func TestIACSearchSourceFileIsSinglePage(t *testing.T) {
	c := &iacClient{sourceFile: "testdata/iac_listing.html"}
	recs, _, err := c.Search(context.Background(), "RU")
	if err != nil {
		t.Fatalf("Search: %v", err)
	}
	if len(recs) == 0 {
		t.Fatal("expected records parsed from the sourceFile fixture")
	}
}

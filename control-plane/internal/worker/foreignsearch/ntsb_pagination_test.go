package foreignsearch

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

// carolPageJSON builds a synthetic CAROL Query/Main response with `count` rows
// (each carrying a unique NtsbNo derived from offset+i) and the given
// server-reported ResultListCount.
func carolPageJSON(offset, count, resultListCount int) []byte {
	type field struct {
		FieldName string   `json:"FieldName"`
		Values    []string `json:"Values"`
	}
	type result struct {
		Fields []field `json:"Fields"`
	}
	type resp struct {
		Results         []result `json:"Results"`
		ResultListCount int      `json:"ResultListCount"`
	}
	r := resp{ResultListCount: resultListCount}
	for i := 0; i < count; i++ {
		ref := fmt.Sprintf("REF%05d", offset+i)
		r.Results = append(r.Results, result{Fields: []field{{FieldName: "NtsbNo", Values: []string{ref}}}})
	}
	b, _ := json.Marshal(r)
	return b
}

// newCarolTestServer returns an httptest server serving /session (always
// session id 1) and /query (delegating to pageFn, keyed by ResultSetOffset),
// plus a thread-safe recorder of every offset requested.
func newCarolTestServer(t *testing.T, pageFn func(offset int) []byte) (*httptest.Server, func() []int) {
	t.Helper()
	var mu sync.Mutex
	var offsets []int
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/session":
			w.Header().Set("Content-Type", "application/json")
			w.Write([]byte("1"))
		case "/query":
			var payload struct {
				ResultSetOffset int `json:"ResultSetOffset"`
			}
			if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			mu.Lock()
			offsets = append(offsets, payload.ResultSetOffset)
			mu.Unlock()
			w.Header().Set("Content-Type", "application/json")
			w.Write(pageFn(payload.ResultSetOffset))
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(ts.Close)
	return ts, func() []int {
		mu.Lock()
		defer mu.Unlock()
		out := make([]int, len(offsets))
		copy(out, offsets)
		return out
	}
}

// TestNTSBSearchPaginatesUntilShortPage is GO-CP-6's core regression guard: a
// full first page (ResultSetSize=500 rows) must not be treated as the whole
// result set — Search must fetch the next offset until a page comes back
// short, then stop.
func TestNTSBSearchPaginatesUntilShortPage(t *testing.T) {
	ts, getOffsets := newCarolTestServer(t, func(offset int) []byte {
		switch offset {
		case 0:
			return carolPageJSON(0, carolPageSize, 650) // full page
		case carolPageSize:
			return carolPageJSON(carolPageSize, 150, 650) // short page: last one
		default:
			t.Errorf("unexpected ResultSetOffset requested: %d", offset)
			return carolPageJSON(offset, 0, 650)
		}
	})

	c := &ntsbClient{http: http.DefaultClient, queryEndpoint: ts.URL + "/query", sessionEndpoint: ts.URL + "/session"}
	recs, err := c.Search(context.Background(), "BS")
	if err != nil {
		t.Fatalf("Search: %v", err)
	}
	if len(recs) != 650 {
		t.Fatalf("len(recs) = %d, want 650 (500+150 across two pages)", len(recs))
	}
	if offsets := getOffsets(); len(offsets) != 2 || offsets[0] != 0 || offsets[1] != carolPageSize {
		t.Fatalf("offsets requested = %v, want [0 %d] (exactly two pages)", offsets, carolPageSize)
	}
	// No duplicate refs across pages.
	seen := map[string]bool{}
	for _, r := range recs {
		if seen[r.ForeignRef] {
			t.Fatalf("duplicate ForeignRef %q across pages", r.ForeignRef)
		}
		seen[r.ForeignRef] = true
	}
}

// TestNTSBSearchSinglePageNoPagination verifies the common case (well under
// 500 results) makes exactly one query request — pagination must not add
// N+1 round trips for small countries.
func TestNTSBSearchSinglePageNoPagination(t *testing.T) {
	ts, getOffsets := newCarolTestServer(t, func(offset int) []byte {
		return carolPageJSON(offset, 20, 20)
	})
	c := &ntsbClient{http: http.DefaultClient, queryEndpoint: ts.URL + "/query", sessionEndpoint: ts.URL + "/session"}
	recs, err := c.Search(context.Background(), "BS")
	if err != nil {
		t.Fatalf("Search: %v", err)
	}
	if len(recs) != 20 {
		t.Fatalf("len(recs) = %d, want 20", len(recs))
	}
	if offsets := getOffsets(); len(offsets) != 1 {
		t.Fatalf("offsets requested = %v, want exactly 1 page", offsets)
	}
}

// TestNTSBSearchHitsPageCapAndWarnsTruncation verifies the carolMaxPages
// safety cap: a server that always returns a full page (never a short one)
// must not loop forever, and — since the server's own ResultListCount then
// disagrees with what was collected — Search must print a CAROL_TRUNCATED
// warning to stderr (GO-CP-6's "record a warning" requirement).
func TestNTSBSearchHitsPageCapAndWarnsTruncation(t *testing.T) {
	const total = 999999 // server claims far more than carolMaxPages*carolPageSize
	ts, getOffsets := newCarolTestServer(t, func(offset int) []byte {
		return carolPageJSON(offset, carolPageSize, total) // always full
	})
	c := &ntsbClient{http: http.DefaultClient, queryEndpoint: ts.URL + "/query", sessionEndpoint: ts.URL + "/session"}

	var recs []ForeignRecord
	out := captureStderr(t, func() {
		var err error
		recs, err = c.Search(context.Background(), "BS")
		if err != nil {
			t.Fatalf("Search: %v", err)
		}
	})

	if want := carolMaxPages * carolPageSize; len(recs) != want {
		t.Fatalf("len(recs) = %d, want %d (capped at carolMaxPages)", len(recs), want)
	}
	if offsets := getOffsets(); len(offsets) != carolMaxPages {
		t.Fatalf("pages fetched = %d, want carolMaxPages=%d (loop must terminate)", len(offsets), carolMaxPages)
	}
	if !strings.Contains(out, "CAROL_TRUNCATED") || !strings.Contains(out, "country=BS") {
		t.Fatalf("expected a CAROL_TRUNCATED warning on stderr, got: %q", out)
	}
}

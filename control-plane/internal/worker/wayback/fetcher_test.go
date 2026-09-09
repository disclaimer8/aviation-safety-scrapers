package wayback

import (
	"context"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
)

// fixtureFetcher is the offline Fetcher used across the package's tests.
type fixtureFetcher struct {
	CDXBody []byte
	Files   map[string][]byte // archivedURL -> bytes
	GetErr  map[string]error  // archivedURL -> error to return
}

func (f *fixtureFetcher) CDX(ctx context.Context, domain string) ([]byte, error) {
	return f.CDXBody, nil
}

func (f *fixtureFetcher) Get(ctx context.Context, archivedURL string) ([]byte, error) {
	if f.GetErr != nil {
		if err, ok := f.GetErr[archivedURL]; ok {
			return nil, err
		}
	}
	if b, ok := f.Files[archivedURL]; ok {
		return b, nil
	}
	return []byte("default-pdf-bytes"), nil
}

// The query is now percent-encoded, so the parameters are checked after
// decoding rather than as literal substrings. Both forms were confirmed to
// return identical results from the live CDX API on 2026-09-09.
func TestCDXURLConstruction(t *testing.T) {
	got := cdxURL("caa.example.gov")
	if !strings.HasPrefix(got, "https://web.archive.org/cdx/search/cdx?") {
		t.Fatalf("cdxURL lost its endpoint: %q", got)
	}
	u, err := url.Parse(got)
	if err != nil {
		t.Fatalf("cdxURL produced an unparseable URL %q: %v", got, err)
	}
	for k, want := range map[string]string{
		"url":      "caa.example.gov/*",
		"output":   "json",
		"filter":   "mimetype:application/pdf",
		"collapse": "digest",
	} {
		if got := u.Query().Get(k); got != want {
			t.Errorf("cdxURL %s = %q, want %q (from %q)", k, got, want, u)
		}
	}
}

// ResolveTarget falls back to authorities.archive_url when a country has no
// wayback_target overlay, and that column holds a URL, not a bare host. A "?"
// or "&" in it used to truncate the CDX query silently: the request still
// returned 200, with zero rows, which reads as "this authority has no PDFs".
func TestCDXURLDoesNotLetATargetTruncateTheQuery(t *testing.T) {
	for _, target := range []string{
		"caa.example.gov/reports?year=2020",
		"caa.example.gov/a&b",
		"caa.example.gov/docs#section",
	} {
		u, err := url.Parse(cdxURL(target))
		if err != nil {
			t.Fatalf("cdxURL(%q) unparseable: %v", target, err)
		}
		q := u.Query()
		if q.Get("collapse") != "digest" {
			t.Errorf("cdxURL(%q) lost the trailing parameters: %q", target, u)
		}
		if want := target + "/*"; q.Get("url") != want {
			t.Errorf("cdxURL(%q) url = %q, want %q", target, q.Get("url"), want)
		}
	}
}

// Compile-time check that *httpFetcher satisfies Fetcher.
var _ Fetcher = (*httpFetcher)(nil)
var _ Fetcher = (*fixtureFetcher)(nil)

// TestHTTPFetcherRejectsOversizedResponse pins GO-CP-10's size cap: the
// previous io.ReadAll(resp.Body) had no bound and would read an arbitrarily
// large response fully into memory. A response exceeding maxFetchBytes must
// now be rejected instead.
func TestHTTPFetcherRejectsOversizedResponse(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		chunk := make([]byte, 1<<20)
		for written := 0; written < maxFetchBytes+(2<<20); written += len(chunk) {
			if _, err := w.Write(chunk); err != nil {
				return
			}
		}
	}))
	defer ts.Close()

	f := &httpFetcher{client: &http.Client{Timeout: 30 * time.Second}}
	if _, err := f.fetch(context.Background(), ts.URL); err == nil {
		t.Fatal("expected fetch to reject a response exceeding maxFetchBytes")
	}
}

package wayback

import (
	"context"
	"strings"
	"testing"
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

func TestCDXURLConstruction(t *testing.T) {
	got := cdxURL("caa.example.gov")
	for _, want := range []string{
		"https://web.archive.org/cdx/search/cdx?",
		"url=caa.example.gov/*",
		"output=json",
		"filter=mimetype:application/pdf",
		"collapse=digest",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("cdxURL missing %q in %q", want, got)
		}
	}
}

// Compile-time check that *httpFetcher satisfies Fetcher.
var _ Fetcher = (*httpFetcher)(nil)
var _ Fetcher = (*fixtureFetcher)(nil)

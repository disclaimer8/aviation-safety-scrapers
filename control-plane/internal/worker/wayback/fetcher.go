package wayback

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"time"
)

// Fetcher is the only network seam in the wayback worker. Production uses
// httpFetcher; tests use a fixtureFetcher.
type Fetcher interface {
	CDX(ctx context.Context, domain string) ([]byte, error)
	Get(ctx context.Context, archivedURL string) ([]byte, error)
}

// maxFetchBytes caps every httpFetcher response (GO-CP-10). Consistent in
// spirit with extract.fetchGuarded's maxReportBytes cap (64 MiB) — the CDX
// JSON index and archived PDF bodies fetched here are the same kind of
// untrusted-size response that package guards against; this package can't
// import extract (extract already imports wayback), so the cap is
// duplicated locally rather than shared.
const maxFetchBytes = 64 << 20

type httpFetcher struct {
	client *http.Client
}

// NewHTTPFetcher returns a Fetcher backed by net/http against web.archive.org.
func NewHTTPFetcher(timeout time.Duration) Fetcher {
	return &httpFetcher{client: &http.Client{Timeout: timeout}}
}

// cdxURL builds the CDX API request URL for a domain. Domains are trusted
// seed/authority values, not user input, so the query string is hand-built to
// keep literal substrings (url=<domain>/*, filter=mimetype:application/pdf)
// readable and testable without URL-encoding.
func cdxURL(domain string) string {
	return "https://web.archive.org/cdx/search/cdx?" +
		"url=" + domain + "/*" +
		"&output=json" +
		"&filter=mimetype:application/pdf" +
		"&collapse=digest"
}

func (h *httpFetcher) CDX(ctx context.Context, domain string) ([]byte, error) {
	return h.fetch(ctx, cdxURL(domain))
}

func (h *httpFetcher) Get(ctx context.Context, archivedURL string) ([]byte, error) {
	return h.fetch(ctx, archivedURL)
}

func (h *httpFetcher) fetch(ctx context.Context, u string) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return nil, fmt.Errorf("wayback: build request %s: %w", u, err)
	}
	resp, err := h.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("wayback: fetch %s: %w", u, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("wayback: fetch %s: status %d", u, resp.StatusCode)
	}
	// Cap the body (GO-CP-10): read one byte beyond the limit so "exactly at
	// the limit" and "exceeded" are distinguishable, and fail explicitly
	// rather than silently truncating or reading an unbounded response into
	// memory.
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxFetchBytes+1))
	if err != nil {
		return nil, fmt.Errorf("wayback: read %s: %w", u, err)
	}
	if len(body) > maxFetchBytes {
		return nil, fmt.Errorf("wayback: fetch %s: response exceeds %d-byte limit", u, maxFetchBytes)
	}
	return body, nil
}

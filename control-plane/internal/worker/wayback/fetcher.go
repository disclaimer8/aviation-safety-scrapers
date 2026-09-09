package wayback

import (
	"context"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/nethard"
)

// Fetcher is the only network seam in the wayback worker. Production uses
// httpFetcher; tests use a fixtureFetcher.
type Fetcher interface {
	CDX(ctx context.Context, domain string) ([]byte, error)
	Get(ctx context.Context, archivedURL string) ([]byte, error)
}

// maxFetchBytes caps every httpFetcher response (GO-CP-10). It used to be
// hand-duplicated from extract because this package cannot import extract
// (extract already imports wayback); both now take it — and the SSRF guard
// and scheme check that were NOT duplicated — from internal/nethard.
const maxFetchBytes = nethard.MaxBodyBytes

type httpFetcher struct {
	client *http.Client
}

// NewHTTPFetcher returns a Fetcher backed by net/http against web.archive.org.
//
// The transport carries the SSRF guard. This client follows redirects, and a
// redirect off archive.org used to be dialled with no private-IP check at all
// — the one fetcher in the control plane without one.
func NewHTTPFetcher(timeout time.Duration) Fetcher {
	return &httpFetcher{client: &http.Client{
		Timeout:   timeout,
		Transport: nethard.HardenedTransport(nil),
	}}
}

// cdxURL builds the CDX API request URL for a domain.
//
// The domain is escaped rather than concatenated. It was called "a trusted
// seed value", but ResolveTarget falls back to authorities.archive_url when a
// country has no wayback_target overlay, and that column holds a URL, not a
// host: a "?" or "&" in it silently truncated the CDX query, which surfaces
// as found=0 / SILENT_FAIL_SUSPECT rather than as an error.
func cdxURL(domain string) string {
	q := url.Values{
		"url":      {strings.TrimSuffix(domain, "/") + "/*"},
		"output":   {"json"},
		"filter":   {"mimetype:application/pdf"},
		"collapse": {"digest"},
	}
	return "https://web.archive.org/cdx/search/cdx?" + q.Encode()
}

func (h *httpFetcher) CDX(ctx context.Context, domain string) ([]byte, error) {
	return h.fetch(ctx, cdxURL(domain))
}

func (h *httpFetcher) Get(ctx context.Context, archivedURL string) ([]byte, error) {
	return h.fetch(ctx, archivedURL)
}

func (h *httpFetcher) fetch(ctx context.Context, u string) ([]byte, error) {
	if err := nethard.CheckScheme(u); err != nil {
		return nil, fmt.Errorf("wayback: fetch: %w", err)
	}
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
	// Cap the body (GO-CP-10): fail explicitly rather than silently truncating
	// or reading an unbounded response into memory.
	body, err := nethard.ReadCapped(resp.Body, "response from "+u)
	if err != nil {
		return nil, fmt.Errorf("wayback: %w", err)
	}
	return body, nil
}

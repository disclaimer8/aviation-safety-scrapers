package extract

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/atomicfile"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/nethard"
)

// maxReportBytes is the maximum allowed response body size (64 MiB).
// Responses larger than this are rejected to prevent disk/RAM exhaustion.
const maxReportBytes = nethard.MaxBodyBytes

// The SSRF guard, the size cap and the scheme check now live in
// internal/nethard so the wayback fetcher — which cannot import this package —
// gets exactly the same policy instead of its own (absent) one.
var (
	isPrivateIP         = nethard.IsPrivateIP
	ssrfSafeDialContext = nethard.SSRFSafeDialContext
	// dialContextOverride stays a package-level var: download_test.go swaps it
	// for a plain dialer so the tests can reach an httptest server on loopback.
	dialContextOverride nethard.DialFunc = nethard.SSRFSafeDialContext
)

func hardenedTransport(base http.RoundTripper) http.RoundTripper {
	return nethard.HardenedTransportWith(base, dialContextOverride)
}

// fetchGuarded performs a scheme-checked, SSRF-guarded, size-capped HTTP GET
// for rawURL using the provided client. It returns the raw response body or a
// descriptive error. It does NOT write any file; that is the caller's job.
//
// Security properties (applied to the html-page path too, not just PDF downloads):
//   - Only http and https are allowed.
//   - The transport is replaced with the SSRF-safe dial context so every hop
//     (including redirects) is checked against the private-IP deny-list.
//   - The response body is capped at maxReportBytes; exceeding the cap returns
//     an error rather than silently truncating.
func fetchGuarded(ctx context.Context, client *http.Client, rawURL string) ([]byte, error) {
	u, err := url.Parse(rawURL)
	if err != nil {
		return nil, fmt.Errorf("extract: fetch: parse URL %q: %w", rawURL, err)
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return nil, fmt.Errorf("extract: fetch: scheme %q not allowed (must be http or https): %s", u.Scheme, rawURL)
	}

	// Build a hardened client: clone the caller's client to inherit Timeout /
	// Jar / CheckRedirect settings, but override the transport with the SSRF
	// guard so every dial (including redirect hops) is checked.
	hardened := *client // shallow copy keeps Timeout, Jar, CheckRedirect
	hardened.Transport = hardenedTransport(client.Transport)

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return nil, fmt.Errorf("extract: fetch: build request %s: %w", rawURL, err)
	}

	resp, err := hardened.Do(req)
	if err != nil {
		return nil, fmt.Errorf("extract: fetch: GET %s: %w", rawURL, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("extract: fetch: GET %s: status %d", rawURL, resp.StatusCode)
	}

	// Cap body to maxReportBytes. Read one byte beyond the limit so we can
	// distinguish "exactly at limit" from "exceeded" and fail explicitly
	// rather than silently truncating.
	limited := io.LimitReader(resp.Body, int64(maxReportBytes)+1)
	body, err := io.ReadAll(limited)
	if err != nil {
		return nil, fmt.Errorf("extract: fetch: read body %s: %w", rawURL, err)
	}
	if len(body) > maxReportBytes {
		return nil, fmt.Errorf("extract: fetch: response body from %s exceeds %d-byte limit", rawURL, maxReportBytes)
	}
	return body, nil
}

// DownloadReportURL fetches rawURL (http/https only), writes the response body
// to <storeDir>/<iso2>/<sha256hex>.pdf via an atomic write, and returns the
// local file path and its SHA-256 hex digest. Non-http(s) schemes, non-200
// responses, SSRF-blocked targets, oversized responses, and I/O failures all
// return a descriptive error that callers can map to download_status='failed'.
func DownloadReportURL(ctx context.Context, client *http.Client, rawURL, storeDir, iso2 string) (localPath, digest string, err error) {
	body, err := fetchGuarded(ctx, client, rawURL)
	if err != nil {
		// Preserve the pre-refactor error prefix style for backward compat with
		// callers that match "extract: download:" in error messages.
		return "", "", fmt.Errorf("extract: download: %w", err)
	}

	sum := sha256.Sum256(body)
	hexDigest := hex.EncodeToString(sum[:])

	dir := filepath.Join(storeDir, iso2)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", "", fmt.Errorf("extract: download: mkdir %s: %w", dir, err)
	}

	destPath := filepath.Join(dir, hexDigest+".pdf")
	if err := atomicfile.Write(destPath, body); err != nil {
		return "", "", fmt.Errorf("extract: download: write %s: %w", destPath, err)
	}

	return destPath, hexDigest, nil
}

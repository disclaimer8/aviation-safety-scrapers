package extract

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/atomicfile"
)

// maxReportBytes is the maximum allowed response body size (64 MiB).
// Responses larger than this are rejected to prevent disk/RAM exhaustion.
const maxReportBytes = 64 << 20 // 64 MiB

// isPrivateIP returns true if ip is loopback, private (RFC1918), link-local
// (169.254/fe80), unique-local (fc00::/7), multicast, or unspecified.
// These are all targets that must never be reached from scraped URLs (SSRF).
func isPrivateIP(ip net.IP) bool {
	if ip.IsLoopback() || ip.IsPrivate() || ip.IsLinkLocalUnicast() ||
		ip.IsLinkLocalMulticast() || ip.IsMulticast() || ip.IsUnspecified() {
		return true
	}
	// fc00::/7 — unique-local IPv6. ip.IsPrivate() covers RFC1918 but in older
	// Go versions may not cover ULA; belt-and-suspenders explicit mask check.
	if ip4 := ip.To4(); ip4 == nil && len(ip) == 16 {
		if ip[0]&0xfe == 0xfc {
			return true
		}
	}
	return false
}

// ssrfSafeDialContext is a DialContext that resolves the hostname and rejects
// any address whose IP falls in a private/internal range before dialing.
// Because every HTTP hop (including redirects) goes through DialContext, this
// guard covers redirect chains automatically.
func ssrfSafeDialContext(ctx context.Context, network, addr string) (net.Conn, error) {
	host, port, err := net.SplitHostPort(addr)
	if err != nil {
		return nil, fmt.Errorf("extract: ssrf-guard: split host/port %q: %w", addr, err)
	}

	ips, err := net.DefaultResolver.LookupIPAddr(ctx, host)
	if err != nil {
		return nil, fmt.Errorf("extract: ssrf-guard: resolve %q: %w", host, err)
	}

	for _, ipAddr := range ips {
		if isPrivateIP(ipAddr.IP) {
			return nil, fmt.Errorf("extract: ssrf-guard: host %q resolves to private/internal IP %s — blocked", host, ipAddr.IP)
		}
	}

	// All IPs passed; dial with the default dialer.
	var d net.Dialer
	return d.DialContext(ctx, network, net.JoinHostPort(host, port))
}

// dialContextOverride is the DialContext used by hardenedTransport. It is set
// to ssrfSafeDialContext by default and may only be overridden in tests
// (via download_test.go) to allow loopback connections to httptest servers.
var dialContextOverride = ssrfSafeDialContext

// hardenedTransport wraps an existing http.RoundTripper (or clones
// http.DefaultTransport) and overrides DialContext with the SSRF guard.
func hardenedTransport(base http.RoundTripper) http.RoundTripper {
	var t *http.Transport
	if bt, ok := base.(*http.Transport); ok && bt != nil {
		t = bt.Clone()
	} else {
		t = http.DefaultTransport.(*http.Transport).Clone()
	}
	t.DialContext = dialContextOverride
	return t
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

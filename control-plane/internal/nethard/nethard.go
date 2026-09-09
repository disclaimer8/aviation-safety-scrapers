// Package nethard holds the hardened HTTP pieces shared by every worker that
// fetches a URL the pipeline did not write itself.
//
// It exists because the guard used to live in internal/worker/extract, which
// the wayback worker cannot import (extract already imports wayback). The cap
// was therefore duplicated by hand and the SSRF guard was not duplicated at
// all: extract blocked RFC1918 and loopback on every hop, and the wayback
// fetcher — which follows redirects off archive.org — blocked nothing.
package nethard

import (
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
)

// MaxBodyBytes caps every response read through this package (GO-CP-10).
const MaxBodyBytes = 64 << 20 // 64 MiB

// IsPrivateIP returns true if ip is loopback, private (RFC1918), link-local
// (169.254/fe80), unique-local (fc00::/7), multicast, or unspecified.
// These are all targets that must never be reached from a scraped URL (SSRF).
func IsPrivateIP(ip net.IP) bool {
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

// SSRFSafeDialContext resolves the hostname, rejects any address in a
// private/internal range, and then dials THE ADDRESS IT CHECKED.
//
// Dialing the checked IP rather than the hostname is the point: the previous
// version resolved the name, validated the answers, and then handed the name
// back to the dialer, which resolved it a second time. A DNS server that
// answers differently between those two lookups (a rebinding attack, or
// simply a short-TTL round-robin) got the connection it wanted. Now only an
// address that passed the check is ever connected to.
//
// Every HTTP hop, redirects included, goes through DialContext, so this covers
// redirect chains automatically.
func SSRFSafeDialContext(ctx context.Context, network, addr string) (net.Conn, error) {
	host, port, err := net.SplitHostPort(addr)
	if err != nil {
		return nil, fmt.Errorf("ssrf-guard: split host/port %q: %w", addr, err)
	}

	ips, err := net.DefaultResolver.LookupIPAddr(ctx, host)
	if err != nil {
		return nil, fmt.Errorf("ssrf-guard: resolve %q: %w", host, err)
	}
	if len(ips) == 0 {
		return nil, fmt.Errorf("ssrf-guard: %q resolved to no addresses", host)
	}

	var d net.Dialer
	var lastErr error
	for _, ipAddr := range ips {
		if IsPrivateIP(ipAddr.IP) {
			return nil, fmt.Errorf("ssrf-guard: host %q resolves to private/internal IP %s — blocked",
				host, ipAddr.IP)
		}
	}
	for _, ipAddr := range ips {
		conn, err := d.DialContext(ctx, network, net.JoinHostPort(ipAddr.IP.String(), port))
		if err == nil {
			return conn, nil
		}
		lastErr = err
	}
	return nil, lastErr
}

// DialFunc is the dialer shape HardenedTransportWith installs.
type DialFunc func(ctx context.Context, network, addr string) (net.Conn, error)

// HardenedTransport clones base (or http.DefaultTransport) and replaces its
// dialer with the SSRF guard.
func HardenedTransport(base http.RoundTripper) http.RoundTripper {
	return HardenedTransportWith(base, SSRFSafeDialContext)
}

// HardenedTransportWith is HardenedTransport with an explicit dialer. Callers
// keep their own override variable so a test can swap in a loopback dialer for
// an httptest server without reaching across packages.
func HardenedTransportWith(base http.RoundTripper, dial DialFunc) http.RoundTripper {
	var t *http.Transport
	if bt, ok := base.(*http.Transport); ok && bt != nil {
		t = bt.Clone()
	} else {
		t = http.DefaultTransport.(*http.Transport).Clone()
	}
	t.DialContext = dial
	return t
}

// CheckScheme rejects anything that is not http or https.
func CheckScheme(rawURL string) error {
	u, err := url.Parse(rawURL)
	if err != nil {
		return fmt.Errorf("parse URL %q: %w", rawURL, err)
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return fmt.Errorf("scheme %q not allowed (must be http or https): %s", u.Scheme, rawURL)
	}
	return nil
}

// ReadCapped reads at most MaxBodyBytes, and fails rather than truncating.
func ReadCapped(r io.Reader, what string) ([]byte, error) {
	body, err := io.ReadAll(io.LimitReader(r, int64(MaxBodyBytes)+1))
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", what, err)
	}
	if len(body) > MaxBodyBytes {
		return nil, fmt.Errorf("%s exceeds %d-byte limit", what, MaxBodyBytes)
	}
	return body, nil
}

package extract

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// allowLoopback replaces dialContextOverride with the default net.Dialer for
// the duration of t, so httptest servers on 127.0.0.1 are reachable.
// It is only used by existing download tests that predate the SSRF guard.
func allowLoopback(t *testing.T) {
	t.Helper()
	orig := dialContextOverride
	var d net.Dialer
	dialContextOverride = d.DialContext
	t.Cleanup(func() { dialContextOverride = orig })
}

func TestDownloadReportURL(t *testing.T) {
	// Existing tests need loopback access to httptest.NewServer.
	allowLoopback(t)

	pdfBytes := []byte("%PDF-1.4 fake pdf content for testing")
	sum := sha256.Sum256(pdfBytes)
	wantDigest := hex.EncodeToString(sum[:])

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/report.pdf":
			w.Header().Set("Content-Type", "application/pdf")
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write(pdfBytes)
		case "/notfound.pdf":
			w.WriteHeader(http.StatusNotFound)
		default:
			w.WriteHeader(http.StatusInternalServerError)
		}
	}))
	defer srv.Close()

	tests := []struct {
		name     string
		rawURL   string
		wantErr  bool
		wantPath bool
		wantDig  string
	}{
		{
			name:     "200 PDF written with correct digest",
			rawURL:   srv.URL + "/report.pdf",
			wantErr:  false,
			wantPath: true,
			wantDig:  wantDigest,
		},
		{
			name:    "javascript scheme rejected without fetch",
			rawURL:  "javascript:alert(1)",
			wantErr: true,
		},
		{
			name:    "ftp scheme rejected without fetch",
			rawURL:  "ftp://example.com/file.pdf",
			wantErr: true,
		},
		{
			name:    "404 response returns error",
			rawURL:  srv.URL + "/notfound.pdf",
			wantErr: true,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			storeDir := t.TempDir()
			client := &http.Client{}
			localPath, digest, err := DownloadReportURL(context.Background(), client, tc.rawURL, storeDir, "ZZ")

			if tc.wantErr {
				if err == nil {
					t.Fatalf("expected error, got localPath=%q digest=%q", localPath, digest)
				}
				return
			}

			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}

			wantFilePath := filepath.Join(storeDir, "ZZ", wantDigest+".pdf")
			if localPath != wantFilePath {
				t.Errorf("localPath = %q, want %q", localPath, wantFilePath)
			}
			if digest != tc.wantDig {
				t.Errorf("digest = %q, want %q", digest, tc.wantDig)
			}

			got, err := os.ReadFile(localPath)
			if err != nil {
				t.Fatalf("read written file: %v", err)
			}
			if string(got) != string(pdfBytes) {
				t.Errorf("file content = %q, want %q", got, pdfBytes)
			}
		})
	}
}

// TestDownloadReportURL_SSRFBlocked verifies that the SSRF guard rejects
// connections to loopback/private IPs. It tests the guard directly so we
// do not need to route an HTTP request through the guard (which the existing
// test suite bypasses via allowLoopback).
func TestDownloadReportURL_SSRFBlocked(t *testing.T) {
	privateAddrs := []struct {
		name string
		ip   net.IP
	}{
		{"loopback IPv4", net.ParseIP("127.0.0.1")},
		{"loopback IPv6", net.ParseIP("::1")},
		{"RFC1918 192.168", net.ParseIP("192.168.1.1")},
		{"RFC1918 10.x", net.ParseIP("10.0.0.1")},
		{"RFC1918 172.16", net.ParseIP("172.16.0.1")},
		{"link-local 169.254", net.ParseIP("169.254.169.254")},
		{"link-local IPv6 fe80", net.ParseIP("fe80::1")},
		{"unique-local fc00", net.ParseIP("fc00::1")},
		{"multicast", net.ParseIP("224.0.0.1")},
		{"unspecified", net.ParseIP("0.0.0.0")},
	}

	for _, tc := range privateAddrs {
		t.Run(tc.name, func(t *testing.T) {
			if !isPrivateIP(tc.ip) {
				t.Errorf("isPrivateIP(%s) = false, want true", tc.ip)
			}
		})
	}

	// Verify a public IP is NOT blocked.
	public := net.ParseIP("1.1.1.1")
	if isPrivateIP(public) {
		t.Errorf("isPrivateIP(%s) = true for public IP, want false", public)
	}

	// Verify ssrfSafeDialContext returns an error for a loopback address.
	// We synthesise a fake addr string — the guard resolves the host.
	// Use 127.0.0.1 directly (no DNS lookup for numeric IPs).
	_, err := ssrfSafeDialContext(context.Background(), "tcp", "127.0.0.1:80")
	if err == nil {
		t.Fatal("expected ssrfSafeDialContext to block 127.0.0.1:80, got nil error")
	}
	if !strings.Contains(err.Error(), "ssrf-guard") {
		t.Errorf("error should mention ssrf-guard, got: %v", err)
	}

	// Verify DownloadReportURL rejects a loopback URL end-to-end (with SSRF guard active).
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("%PDF-1.4 should not be fetched"))
	}))
	defer srv.Close()

	storeDir := t.TempDir()
	client := &http.Client{}
	// dialContextOverride is ssrfSafeDialContext (default) — no allowLoopback here.
	localPath, digest, dlErr := DownloadReportURL(context.Background(), client, srv.URL+"/secret.pdf", storeDir, "ZZ")
	if dlErr == nil {
		t.Fatalf("expected SSRF block, got localPath=%q digest=%q", localPath, digest)
	}

	// Confirm no file was written under storeDir.
	entries, _ := os.ReadDir(filepath.Join(storeDir, "ZZ"))
	if len(entries) > 0 {
		t.Errorf("expected no files written after SSRF block, got %d file(s)", len(entries))
	}
}

// TestDownloadReportURL_OversizedResponse verifies that a response body larger
// than maxReportBytes causes a clean error without silent truncation.
// The size-limit logic is tested via a unit-level helper that mirrors the
// LimitReader path in DownloadReportURL (we cannot serve oversized responses
// through DownloadReportURL itself in tests because the SSRF guard blocks
// loopback httptest servers unless allowLoopback is set, and allocating 64 MiB
// in a table-test sub-process is expensive — the logic itself is trivial).
func TestDownloadReportURL_OversizedResponse(t *testing.T) {
	exceedsLimit := func(data []byte, limit int) bool {
		limited := io.LimitReader(bytes.NewReader(data), int64(limit)+1)
		body, err := io.ReadAll(limited)
		if err != nil {
			return false
		}
		return len(body) > limit
	}

	const smallLimit = 16 // use a tiny limit so the test stays cheap

	t.Run("body over limit is detected", func(t *testing.T) {
		data := bytes.Repeat([]byte("A"), smallLimit+1)
		if !exceedsLimit(data, smallLimit) {
			t.Error("expected body exceeding limit to be detected")
		}
	})

	t.Run("body exactly at limit is accepted", func(t *testing.T) {
		data := bytes.Repeat([]byte("A"), smallLimit)
		if exceedsLimit(data, smallLimit) {
			t.Error("expected body at limit to be accepted")
		}
	})

	t.Run("body under limit is accepted", func(t *testing.T) {
		data := bytes.Repeat([]byte("A"), smallLimit-1)
		if exceedsLimit(data, smallLimit) {
			t.Error("expected body under limit to be accepted")
		}
	})

	// End-to-end: serve slightly over the limit through DownloadReportURL using
	// allowLoopback so the httptest server is reachable.
	t.Run("end-to-end oversized returns error no file written", func(t *testing.T) {
		const tinyLimit = 32
		orig := maxReportBytes // save — cannot reassign const, so we test via
		// the same logic path but with a handler that serves tinyLimit+1 bytes
		// and verify that the limit guard triggers when maxReportBytes is small.
		// Since maxReportBytes is a const we can't mutate it here; instead we
		// test the behaviour at the real limit using allowLoopback.
		_ = orig

		// Serve exactly maxReportBytes+1 bytes.
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusOK)
			chunk := bytes.Repeat([]byte("Z"), 4096)
			written := 0
			total := maxReportBytes + 1
			for written < total {
				n := total - written
				if n > len(chunk) {
					n = len(chunk)
				}
				_, _ = w.Write(chunk[:n])
				written += n
			}
		}))
		defer srv.Close()

		// Need loopback access for httptest.
		origDial := dialContextOverride
		var d net.Dialer
		dialContextOverride = d.DialContext
		defer func() { dialContextOverride = origDial }()

		storeDir := t.TempDir()
		client := &http.Client{}
		localPath, digest, err := DownloadReportURL(context.Background(), client, srv.URL+"/big.pdf", storeDir, "ZZ")
		if err == nil {
			t.Fatalf("expected oversized-body error, got localPath=%q digest=%q", localPath, digest)
		}
		if !strings.Contains(err.Error(), "exceeds") && !strings.Contains(err.Error(), "limit") {
			t.Errorf("error should mention size limit, got: %v", err)
		}

		// Confirm no file was written.
		entries, _ := os.ReadDir(fmt.Sprintf("%s/ZZ", storeDir))
		if len(entries) > 0 {
			t.Errorf("expected no files written for oversized response, got %d file(s)", len(entries))
		}
	})
}

package manufacturer

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// TestDiscover_SourceFile checks that Discover reads from the local fixture
// without making any network calls when SourceFile is set.
func TestDiscover_SourceFile(t *testing.T) {
	c := NewClient(0, "testdata/safetyfirst_listing.html")
	recs, err := c.Discover(context.Background())
	if err != nil {
		t.Fatalf("Discover error: %v", err)
	}
	if len(recs) != 43 {
		t.Fatalf("expected exactly 43 records (frozen fixture), got %d", len(recs))
	}
	// Issue 41 must be present.
	var found bool
	for _, r := range recs {
		if r.IssueRef == "41" {
			found = true
			if !strings.HasSuffix(r.ReportURL, "safety_first_41.pdf") {
				t.Errorf("issue 41 ReportURL unexpected: %q", r.ReportURL)
			}
		}
	}
	if !found {
		t.Fatal("issue #41 not found in Discover results")
	}
}

// TestProbeNextIssue_Found verifies that ProbeNextIssue returns a record when
// the server responds 200 application/pdf for the next issue PDF.
func TestProbeNextIssue_Found(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Only match the expected next-issue filename.
		if strings.HasSuffix(r.URL.Path, "safety_first_42.pdf") {
			w.Header().Set("Content-Type", "application/pdf")
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte("%PDF-1.4 fake"))
			return
		}
		http.NotFound(w, r)
	}))
	defer ts.Close()

	c := NewClient(0, "")
	// Override the probe base URL so it points at our test server.
	c.ProbeBaseURL = ts.URL + "/pdf/safety+first/"

	rec, ok, err := c.ProbeNextIssue(context.Background(), 41)
	if err != nil {
		t.Fatalf("ProbeNextIssue error: %v", err)
	}
	if !ok {
		t.Fatal("expected ok=true for 200 application/pdf response")
	}
	if rec.IssueRef != "42" {
		t.Errorf("expected IssueRef=42, got %q", rec.IssueRef)
	}
	if !strings.HasSuffix(rec.ReportURL, "safety_first_42.pdf") {
		t.Errorf("expected ReportURL ending in safety_first_42.pdf, got %q", rec.ReportURL)
	}
}

// TestProbeNextIssue_NotFound verifies that ProbeNextIssue returns (_, false, nil)
// when the server responds 404.
func TestProbeNextIssue_NotFound(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.NotFound(w, r)
	}))
	defer ts.Close()

	c := NewClient(0, "")
	c.ProbeBaseURL = ts.URL + "/pdf/safety+first/"

	_, ok, err := c.ProbeNextIssue(context.Background(), 41)
	if err != nil {
		t.Fatalf("ProbeNextIssue error: %v", err)
	}
	if ok {
		t.Fatal("expected ok=false for 404 response")
	}
}

// TestDiscover_TruncatedBodyReturnsError is GO-CP-8's core regression guard:
// the previous hand-rolled read loop treated ANY Read error (including a
// connection dropped mid-response) the same as a clean io.EOF and silently
// parsed whatever partial bytes it had as if it were the complete listing.
// Here the server declares a Content-Length far larger than what it actually
// sends, then closes the connection — Discover must surface an error instead
// of returning success with a truncated (and thus incomplete/misleading)
// record set.
func TestDiscover_TruncatedBodyReturnsError(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()

	go func() {
		conn, acceptErr := ln.Accept()
		if acceptErr != nil {
			return
		}
		defer conn.Close()
		buf := make([]byte, 4096)
		_, _ = conn.Read(buf) // drain the request, best-effort

		body := "<html><body>truncated listing content</body>"
		resp := fmt.Sprintf("HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s",
			len(body)+10_000, body) // declared length is never actually sent
		_, _ = conn.Write([]byte(resp))
		// conn.Close() via defer happens here, well short of Content-Length.
	}()

	c := NewClient(0, "")
	c.ListingURL = "http://" + ln.Addr().String() + "/"

	if _, err := c.Discover(context.Background()); err == nil {
		t.Fatal("expected Discover to return an error for a body truncated mid-stream " +
			"(declared Content-Length not met before the connection closed) — " +
			"treating this the same as a clean end-of-body silently parses a broken " +
			"page as the complete listing (GO-CP-8)")
	}
}

// TestDiscover_OversizedBodyReturnsError pins the GO-CP-8 size cap: a listing
// response beyond maxListingBytes must be rejected rather than read
// unbounded into memory.
func TestDiscover_OversizedBodyReturnsError(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html")
		chunk := make([]byte, 1<<20)
		for i := range chunk {
			chunk[i] = 'a'
		}
		for written := 0; written < maxListingBytes+(2<<20); written += len(chunk) {
			if _, err := w.Write(chunk); err != nil {
				return
			}
		}
	}))
	defer ts.Close()

	c := NewClient(0, "")
	c.ListingURL = ts.URL

	if _, err := c.Discover(context.Background()); err == nil {
		t.Fatal("expected Discover to return an error for a response exceeding maxListingBytes")
	}
}

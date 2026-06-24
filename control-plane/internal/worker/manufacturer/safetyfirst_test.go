package manufacturer

import (
	"context"
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
	if len(recs) < 43 {
		t.Fatalf("expected at least 43 records, got %d", len(recs))
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

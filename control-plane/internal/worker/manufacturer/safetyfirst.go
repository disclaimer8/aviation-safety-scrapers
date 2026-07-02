package manufacturer

// safetyfirst.go — HTTP client for the Airbus Safety First magazine listing.
//
// PDF URL pattern derived from the live fixture
// (testdata/safetyfirst_listing.html), where every numbered issue links to:
//
//	https://mms-safetyfirst.s3.eu-west-3.amazonaws.com/pdf/safety+first/safety_first_<N>.pdf
//
// ProbeNextIssue constructs the candidate URL for issue (highestKnown+1) using
// that same pattern via ProbeBaseURL, which defaults to the S3 base.

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"
)

const (
	defaultListingURL = "https://safetyfirst.airbus.com/magazine/"

	// defaultProbeBaseURL is the S3 base path for numbered issue PDFs, derived
	// from real ReportURLs in the fixture.
	defaultProbeBaseURL = "https://mms-safetyfirst.s3.eu-west-3.amazonaws.com/pdf/safety+first/"

	// maxListingBytes caps the Safety First listing page response (GO-CP-8).
	// The real page is a few hundred KB of HTML; 16 MiB is generous headroom
	// while still bounding a pathological/hostile response.
	maxListingBytes = 16 << 20
)

// Client fetches and parses the Airbus Safety First magazine listing.
type Client struct {
	HTTP *http.Client

	// SourceFile, when non-empty, causes Discover to read HTML from a local
	// file instead of making an HTTP request (useful for offline tests and
	// fixture-driven operation).
	SourceFile string

	// ListingURL is the magazine listing page URL.  Defaults to
	// https://safetyfirst.airbus.com/magazine/
	ListingURL string

	// ProbeBaseURL is the S3 directory URL used by ProbeNextIssue to build the
	// candidate PDF URL.  Defaults to the real S3 base; override in tests.
	ProbeBaseURL string
}

// NewClient creates a Client with the given HTTP timeout and optional source
// file path.  Pass timeout=0 to use Go's default (no timeout).
func NewClient(timeout time.Duration, sourceFile string) *Client {
	t := http.DefaultClient
	if timeout > 0 {
		t = &http.Client{Timeout: timeout}
	}
	return &Client{
		HTTP:         t,
		SourceFile:   sourceFile,
		ListingURL:   defaultListingURL,
		ProbeBaseURL: defaultProbeBaseURL,
	}
}

// Discover returns all ManufacturerRecord items found in the Safety First
// magazine listing.  If SourceFile is set, it reads the HTML from that file;
// otherwise it GETs ListingURL.
func (c *Client) Discover(ctx context.Context) ([]ManufacturerRecord, error) {
	var html []byte

	if c.SourceFile != "" {
		data, err := os.ReadFile(c.SourceFile)
		if err != nil {
			return nil, fmt.Errorf("safetyfirst: read source file %q: %w", c.SourceFile, err)
		}
		html = data
	} else {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.ListingURL, nil)
		if err != nil {
			return nil, fmt.Errorf("safetyfirst: build request: %w", err)
		}
		resp, err := c.HTTP.Do(req)
		if err != nil {
			return nil, fmt.Errorf("safetyfirst: GET %s: %w", c.ListingURL, err)
		}
		defer resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			return nil, fmt.Errorf("safetyfirst: GET %s: status %d", c.ListingURL, resp.StatusCode)
		}
		// GO-CP-8: the previous hand-rolled read loop broke out of the loop on
		// ANY Read error, including a dropped connection mid-response — which
		// is indistinguishable from a truncated page's error from a clean
		// io.EOF and gets silently parsed as if it were the complete listing.
		// io.ReadAll only treats io.EOF as "done"; any other error (e.g.
		// io.ErrUnexpectedEOF from a connection closed before Content-Length
		// bytes arrived) is returned to the caller instead of being masked as
		// success. The LimitReader also bounds an unbounded read.
		body, err := io.ReadAll(io.LimitReader(resp.Body, maxListingBytes+1))
		if err != nil {
			return nil, fmt.Errorf("safetyfirst: GET %s: read body: %w", c.ListingURL, err)
		}
		if len(body) > maxListingBytes {
			return nil, fmt.Errorf("safetyfirst: GET %s: response exceeds %d-byte limit", c.ListingURL, maxListingBytes)
		}
		html = body
	}

	return ParseSafetyFirstListing(html, c.ListingURL)
}

// ProbeNextIssue probes for the next numbered issue PDF (highestKnown+1).
// It builds the candidate URL as:
//
//	<ProbeBaseURL>safety_first_<N>.pdf
//
// where N = highestKnown+1 (no zero-padding; the real listing uses bare integers).
//
// Returns (record, true, nil) if the server responds 200 with Content-Type
// application/pdf.  Returns (ManufacturerRecord{}, false, nil) for any non-200
// or non-pdf response.
//
// A scheme guard rejects any ProbeBaseURL that is not http or https.
func (c *Client) ProbeNextIssue(ctx context.Context, highestKnown int) (ManufacturerRecord, bool, error) {
	next := highestKnown + 1
	filename := fmt.Sprintf("safety_first_%d.pdf", next)
	pdfURL := c.ProbeBaseURL + filename

	// Scheme guard.
	if !strings.HasPrefix(pdfURL, "http://") && !strings.HasPrefix(pdfURL, "https://") {
		return ManufacturerRecord{}, false, fmt.Errorf("safetyfirst: probe URL has non-http scheme: %s", pdfURL)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, pdfURL, nil)
	if err != nil {
		return ManufacturerRecord{}, false, fmt.Errorf("safetyfirst: build probe request: %w", err)
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return ManufacturerRecord{}, false, fmt.Errorf("safetyfirst: probe GET %s: %w", pdfURL, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return ManufacturerRecord{}, false, nil
	}

	ct := resp.Header.Get("Content-Type")
	if !strings.Contains(ct, "application/pdf") {
		return ManufacturerRecord{}, false, nil
	}

	ref := fmt.Sprintf("%d", next)
	rec := ManufacturerRecord{
		IssueRef:    ref,
		Title:       fmt.Sprintf("Safety First #%d", next),
		OriginalURL: pdfURL,
		ReportURL:   pdfURL,
	}
	return rec, true, nil
}

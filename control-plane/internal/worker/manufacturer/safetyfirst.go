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
		buf := make([]byte, 0, 1<<20)
		tmp := make([]byte, 32*1024)
		for {
			n, err := resp.Body.Read(tmp)
			if n > 0 {
				buf = append(buf, tmp[:n]...)
			}
			if err != nil {
				break
			}
		}
		html = buf
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

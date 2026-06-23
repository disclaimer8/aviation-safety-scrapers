package regional

import (
	"context"
	"fmt"
	"time"
)

// ECCAA (Eastern Caribbean Civil Aviation Authority, eccaa.org) covers the OECS
// member states. Its TLS/redirect chain is unstable from data-centre IPs, so
// production commonly runs this body out-of-band via --source-file; the live URL
// is a best-effort fallback.
const (
	eccaaListingURL = "https://www.eccaa.org/investigations"
	eccaaBase       = "https://www.eccaa.org"
	eccaaHost       = "eccaa.org"
)

type eccaaClient struct {
	timeout        time.Duration
	sourceFile     string
	renderEndpoint string
}

// NewECCAAClient returns a RegionalClient for ECCAA. When sourceFile is set the
// listing is read from disk (out-of-band); otherwise it is fetched live.
func NewECCAAClient(timeout time.Duration, sourceFile, renderEndpoint string) RegionalClient {
	return &eccaaClient{timeout: timeout, sourceFile: sourceFile, renderEndpoint: renderEndpoint}
}

func (c *eccaaClient) Search(ctx context.Context, countryISO2 string) ([]RegionalRecord, int, error) {
	raw, err := loadListing(ctx, c.timeout, c.sourceFile, c.renderEndpoint, eccaaListingURL)
	if err != nil {
		return nil, 0, fmt.Errorf("regional: ECCAA search: %w", err)
	}
	recs, warnings, err := parseECCAA(raw)
	if err != nil {
		return nil, 0, fmt.Errorf("regional: ECCAA search: %w", err)
	}
	return recs, warnings, nil
}

// parseECCAA extracts report records from an eccaa.org investigations listing.
func parseECCAA(raw []byte) ([]RegionalRecord, int, error) {
	return parseListing(raw, eccaaBase, func(abs string) bool {
		return hostMatch(abs, eccaaHost) && looksLikeReport(abs)
	})
}

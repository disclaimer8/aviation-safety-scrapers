package regional

import (
	"context"
	"fmt"
	"time"
)

// BAGAIA is the accident-investigation arm of BAGASOO (Banjul Accord Group,
// bagasoo.org), covering its West-African member states. The WordPress site
// exposes no stable public report index from data-centre IPs, so production
// commonly runs this body out-of-band via --source-file; the live URL is a
// best-effort fallback.
const (
	bagaiaListingURL = "https://www.bagasoo.org/investigation-reports/"
	bagaiaBase       = "https://www.bagasoo.org"
	bagaiaHost       = "bagasoo.org"
)

type bagaiaClient struct {
	timeout        time.Duration
	sourceFile     string
	renderEndpoint string
}

// NewBAGAIAClient returns a RegionalClient for BAGAIA. When sourceFile is set the
// listing is read from disk (out-of-band); otherwise it is fetched live.
func NewBAGAIAClient(timeout time.Duration, sourceFile, renderEndpoint string) RegionalClient {
	return &bagaiaClient{timeout: timeout, sourceFile: sourceFile, renderEndpoint: renderEndpoint}
}

func (c *bagaiaClient) Search(ctx context.Context, countryISO2 string) ([]RegionalRecord, int, error) {
	raw, err := loadListing(ctx, c.timeout, c.sourceFile, c.renderEndpoint, bagaiaListingURL)
	if err != nil {
		return nil, 0, fmt.Errorf("regional: BAGAIA search: %w", err)
	}
	recs, warnings, err := parseBAGAIA(raw)
	if err != nil {
		return nil, 0, fmt.Errorf("regional: BAGAIA search: %w", err)
	}
	return recs, warnings, nil
}

// parseBAGAIA extracts report records from a bagasoo.org report listing.
func parseBAGAIA(raw []byte) ([]RegionalRecord, int, error) {
	return parseListing(raw, bagaiaBase, func(abs string) bool {
		return hostMatch(abs, bagaiaHost) && looksLikeReport(abs)
	})
}

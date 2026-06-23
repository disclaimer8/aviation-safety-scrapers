package regional

import (
	"context"
	"fmt"
	"time"
)

// IAC (Interstate Aviation Committee / МАК) publishes investigation reports at
// mak.aero. The listing renders client-side (Wix), so production typically runs
// this body out-of-band via --source-file from an operator browser export; the
// live URL is kept as a best-effort fallback.
const (
	iacListingURL = "https://www.mak.aero/rassledovaniya/"
	iacBase       = "https://www.mak.aero"
	iacHost       = "mak.aero"
)

type iacClient struct {
	timeout    time.Duration
	sourceFile string
}

// NewIACClient returns a RegionalClient for the IAC. When sourceFile is set the
// listing is read from disk (out-of-band); otherwise it is fetched live.
func NewIACClient(timeout time.Duration, sourceFile string) RegionalClient {
	return &iacClient{timeout: timeout, sourceFile: sourceFile}
}

func (c *iacClient) Search(ctx context.Context, countryISO2 string) ([]RegionalRecord, int, error) {
	raw, err := loadListing(ctx, c.timeout, c.sourceFile, iacListingURL)
	if err != nil {
		return nil, 0, fmt.Errorf("regional: IAC search: %w", err)
	}
	recs, warnings, err := parseIAC(raw)
	if err != nil {
		return nil, 0, fmt.Errorf("regional: IAC search: %w", err)
	}
	return recs, warnings, nil
}

// parseIAC extracts report records from a mak.aero listing.
func parseIAC(raw []byte) ([]RegionalRecord, int, error) {
	return parseListing(raw, iacBase, func(abs string) bool {
		return hostMatch(abs, iacHost) && looksLikeReport(abs)
	})
}

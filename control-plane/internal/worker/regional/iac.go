package regional

import (
	"context"
	"fmt"
	"net/url"
	"strings"
	"time"
)

// IAC (Interstate Aviation Committee / МАК) publishes investigation reports at
// mak-iac.org/rassledovaniya/ (Bitrix CMS, server-rendered). The site is reached
// via the minipc browser-render service (--render-endpoint) or an operator export
// (--source-file); a plain live fetch is the best-effort fallback.
//
// NB: mak.aero is an unrelated "MAK Aviation Services" company, NOT the IAC.
const (
	iacListingURL = "https://mak-iac.org/rassledovaniya/"
	iacBase       = "https://mak-iac.org"
	iacHost       = "mak-iac.org"
)

type iacClient struct {
	timeout        time.Duration
	sourceFile     string
	renderEndpoint string
}

// NewIACClient returns a RegionalClient for the IAC. When sourceFile is set the
// listing is read from disk (out-of-band); otherwise it is fetched live.
func NewIACClient(timeout time.Duration, sourceFile, renderEndpoint string) RegionalClient {
	return &iacClient{timeout: timeout, sourceFile: sourceFile, renderEndpoint: renderEndpoint}
}

func (c *iacClient) Search(ctx context.Context, countryISO2 string) ([]RegionalRecord, int, error) {
	raw, err := loadListing(ctx, c.timeout, c.sourceFile, c.renderEndpoint, iacListingURL)
	if err != nil {
		return nil, 0, fmt.Errorf("regional: IAC search: %w", err)
	}
	recs, warnings, err := parseIAC(raw)
	if err != nil {
		return nil, 0, fmt.Errorf("regional: IAC search: %w", err)
	}
	return recs, warnings, nil
}

// parseIAC extracts report records from a mak-iac.org listing. Report entries
// live under /rassledovaniya/ with a date-bearing slug
// (e.g. /rassledovaniya/an-2-ra-40440-19-05-2026/); the year-in-path requirement
// excludes the section's navigation pages (o-komissii, bezopasnost-poletov, …).
func parseIAC(raw []byte) ([]RegionalRecord, int, error) {
	return parseListing(raw, iacBase, func(abs string) bool {
		u, err := url.Parse(abs)
		if err != nil {
			return false
		}
		return hostMatch(abs, iacHost) &&
			strings.Contains(strings.ToLower(u.Path), "/rassledovaniya/") &&
			yearRe.MatchString(u.Path)
	})
}

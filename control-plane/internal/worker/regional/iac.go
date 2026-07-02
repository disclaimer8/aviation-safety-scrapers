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

	// iacMaxPages caps Bitrix PAGEN_1 pagination (GO-CP-10). The listing is
	// walked until a page yields zero NEW refs (relative to everything seen
	// so far this run) or this cap is hit, whichever comes first — a sane
	// ceiling against a pagination bug looping forever.
	iacMaxPages = 50

	// iacDefaultPageDelay is slept between pages (not before the first) to
	// stay polite against mak-iac.org. Overridable (0) in tests via
	// iacClient.pageDelay.
	iacDefaultPageDelay = 1 * time.Second
)

type iacClient struct {
	timeout        time.Duration
	sourceFile     string
	renderEndpoint string
	// pageDelay is slept between pages (not before the first). NewIACClient
	// sets it to iacDefaultPageDelay; tests construct &iacClient{...} directly
	// with pageDelay left at its zero value to run instantly.
	pageDelay time.Duration
}

// NewIACClient returns a RegionalClient for the IAC. When sourceFile is set the
// listing is read from disk (out-of-band, single page — an operator export is
// a point-in-time snapshot, not a live paginated crawl); otherwise the live
// listing is fetched and paginated (GO-CP-10).
func NewIACClient(timeout time.Duration, sourceFile, renderEndpoint string) RegionalClient {
	return &iacClient{timeout: timeout, sourceFile: sourceFile, renderEndpoint: renderEndpoint, pageDelay: iacDefaultPageDelay}
}

// Search walks mak-iac.org/rassledovaniya/'s Bitrix PAGEN_1 pagination
// (?PAGEN_1=2, ?PAGEN_1=3, ...) starting from page 1, stopping when a page
// contributes zero refs not already seen this run or when iacMaxPages is
// reached (GO-CP-10: previously only page 1 was ever fetched, capping
// coverage at the ~21 newest reports regardless of how many the site holds —
// staging is dedup'd on body_code+ref, so re-walking already-known pages on a
// later run is idempotent).
//
// An out-of-band sourceFile export is a single-page point-in-time snapshot
// (there is no "next page" to fetch from a local file) and is not paginated.
func (c *iacClient) Search(ctx context.Context, countryISO2 string) ([]RegionalRecord, int, error) {
	if c.sourceFile != "" {
		raw, err := loadListing(ctx, c.timeout, c.sourceFile, c.renderEndpoint, iacListingURL)
		if err != nil {
			return nil, 0, fmt.Errorf("regional: IAC search: %w", err)
		}
		return parseIACRaw(raw)
	}

	seen := map[string]bool{}
	var all []RegionalRecord
	totalWarnings := 0
	for page := 1; page <= iacMaxPages; page++ {
		if page > 1 && c.pageDelay > 0 {
			select {
			case <-ctx.Done():
				return nil, 0, ctx.Err()
			case <-time.After(c.pageDelay):
			}
		}

		pageURL := iacListingURL
		if page > 1 {
			pageURL = fmt.Sprintf("%s?PAGEN_1=%d", iacListingURL, page)
		}
		raw, err := loadListing(ctx, c.timeout, "", c.renderEndpoint, pageURL)
		if err != nil {
			return nil, 0, fmt.Errorf("regional: IAC search: page %d: %w", page, err)
		}
		recs, warnings, err := parseIAC(raw)
		if err != nil {
			return nil, 0, fmt.Errorf("regional: IAC search: page %d: %w", page, err)
		}
		totalWarnings += warnings

		newOnPage := 0
		for _, r := range recs {
			if seen[r.Ref] {
				continue
			}
			seen[r.Ref] = true
			all = append(all, r)
			newOnPage++
		}
		if newOnPage == 0 {
			break
		}
	}
	return all, totalWarnings, nil
}

// parseIACRaw is the non-paginated single-page path shared by the
// out-of-band sourceFile case.
func parseIACRaw(raw []byte) ([]RegionalRecord, int, error) {
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

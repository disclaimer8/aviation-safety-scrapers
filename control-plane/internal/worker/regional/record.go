// Package regional is the regional-body acquisition worker: it drains
// archive_crawl jobs for regional_raio countries by querying the regional
// investigation body (ECCAA/BAGAIA/IAC) and staging the discovered records.
package regional

import "context"

// RegionalRecord is one accident record discovered at a regional body.
type RegionalRecord struct {
	Ref            string // the body's stable record id / report slug
	Title          string
	OccurrenceDate string // ISO yyyy-mm-dd when known, else ""
	OriginalURL    string // the human page for the record
	ReportURL      string // direct report/PDF URL when present, else ""
	Mimetype       string
	// CountryISO2 is the occurrence country when the listing itself carries a
	// deterministic per-record indicator (e.g. a member-state column/field the
	// body publishes alongside each entry). Empty when the listing is body-wide
	// with no such signal, which is the case for every parser currently wired
	// (ECCAA/BAGAIA/IAC parse only title/URL/date; none carry a per-record
	// country field yet). StageRecords prefers this over the crawling job's
	// country whenever it resolves to a known ISO2.
	CountryISO2 string
}

// RegionalClient queries one regional body for accidents in a member country.
// It returns the discovered records and a count of warnings (listing entries
// that matched but lacked a usable title/ref); a warning marks the job partial.
type RegionalClient interface {
	Search(ctx context.Context, countryISO2 string) ([]RegionalRecord, int, error)
}

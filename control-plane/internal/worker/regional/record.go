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
}

// RegionalClient queries one regional body for accidents in a member country.
// It returns the discovered records and a count of warnings (listing entries
// that matched but lacked a usable title/ref); a warning marks the job partial.
type RegionalClient interface {
	Search(ctx context.Context, countryISO2 string) ([]RegionalRecord, int, error)
}

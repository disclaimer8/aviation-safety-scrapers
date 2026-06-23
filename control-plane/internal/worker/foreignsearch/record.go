// Package foreignsearch is the foreign-investigation acquisition worker: it
// drains ntsb/bea/atsb_foreign_search crawl jobs by querying the delegated
// authority's accident records for the occurrence country and staging them.
package foreignsearch

import "context"

// ForeignRecord is one accident record discovered at a foreign authority.
type ForeignRecord struct {
	ForeignRef     string // the authority's stable case/record id
	Title          string
	OccurrenceDate string // ISO yyyy-mm-dd when known, else ""
	OriginalURL    string // the human page for the record
	ReportURL      string // direct report/PDF URL when present, else ""
	Mimetype       string // of ReportURL when known, else ""
}

// AuthorityClient queries one foreign authority for accidents in a country.
type AuthorityClient interface {
	Search(ctx context.Context, countryISO2 string) ([]ForeignRecord, error)
}

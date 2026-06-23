package foreignsearch

import (
	"context"
	"testing"
)

// fixtureClient is the offline AuthorityClient used across the package's tests.
type fixtureClient struct {
	Records []ForeignRecord
	Err     error
}

func (f *fixtureClient) Search(ctx context.Context, countryISO2 string) ([]ForeignRecord, error) {
	if f.Err != nil {
		return nil, f.Err
	}
	return f.Records, nil
}

var _ AuthorityClient = (*fixtureClient)(nil)

func TestForeignRecordFields(t *testing.T) {
	r := ForeignRecord{ForeignRef: "CEN20LA001", Title: "A", OccurrenceDate: "2020-01-02",
		OriginalURL: "https://ntsb/x", ReportURL: "https://ntsb/x.pdf", Mimetype: "application/pdf"}
	if r.ForeignRef == "" || r.Title == "" || r.OriginalURL == "" {
		t.Fatal("ForeignRecord required fields must be settable")
	}
}

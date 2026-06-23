package regional

import (
	"context"
	"testing"
)

type fixtureClient struct {
	Records []RegionalRecord
	Err     error
}

func (f *fixtureClient) Search(ctx context.Context, countryISO2 string) ([]RegionalRecord, error) {
	if f.Err != nil {
		return nil, f.Err
	}
	return f.Records, nil
}

var _ RegionalClient = (*fixtureClient)(nil)

func TestRegionalRecordFields(t *testing.T) {
	r := RegionalRecord{Ref: "2024-RA-01", Title: "A", OccurrenceDate: "2024-01-02",
		OriginalURL: "https://mak.aero/x", ReportURL: "https://mak.aero/x.pdf", Mimetype: "application/pdf"}
	if r.Ref == "" || r.Title == "" || r.OriginalURL == "" {
		t.Fatal("RegionalRecord required fields must be settable")
	}
}

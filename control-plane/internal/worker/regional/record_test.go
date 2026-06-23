package regional

import (
	"context"
	"testing"
)

type fixtureClient struct {
	Records  []RegionalRecord
	Warnings int
	Err      error
}

func (f *fixtureClient) Search(ctx context.Context, countryISO2 string) ([]RegionalRecord, int, error) {
	if f.Err != nil {
		return nil, 0, f.Err
	}
	return f.Records, f.Warnings, nil
}

var _ RegionalClient = (*fixtureClient)(nil)

func TestRegionalRecordFields(t *testing.T) {
	r := RegionalRecord{Ref: "2024-RA-01", Title: "A", OccurrenceDate: "2024-01-02",
		OriginalURL: "https://mak.aero/x", ReportURL: "https://mak.aero/x.pdf", Mimetype: "application/pdf"}
	if r.Ref == "" || r.Title == "" || r.OriginalURL == "" {
		t.Fatal("RegionalRecord required fields must be settable")
	}
}

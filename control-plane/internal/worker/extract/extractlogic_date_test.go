package extract

import "testing"

// Nothing validated the model's date before: the JSON schema requires the key
// but allows any string, so "yesterday" satisfied HasCriticalFields, reached
// events.date, and then keyed dedup — which links globally on (date,
// registration). An invented date is worse than no date.
func TestNormalizeEventRejectsUnparseableDates(t *testing.T) {
	for _, bad := range []string{
		"yesterday",
		"circa 1998",
		"2019-13-45",
		"March 10, 2019",
		"10/03/2019",
		"2019-02-30",
		"1492-01-01",
	} {
		t.Run(bad, func(t *testing.T) {
			got := NormalizeEvent(ExtractedEvent{
				IsAviationAccident: true, Date: bad, DatePrecision: "exact",
				AircraftRegistration: "ET-AVJ",
			})
			if got.Date != "" {
				t.Errorf("Date = %q, want cleared", got.Date)
			}
			if got.DatePrecision != "unknown" {
				t.Errorf("DatePrecision = %q, want unknown", got.DatePrecision)
			}
			if HasCriticalFields(got) {
				t.Error("an unparseable date must not pass the promotion gate")
			}
		})
	}
}

func TestNormalizeEventKeepsWellFormedDates(t *testing.T) {
	for _, tc := range []struct{ date, precision string }{
		{"2019-03-10", "exact"},
		{"2019-03", "month"},
		{"2019", "year"},
		{"2024-02-29", "exact"}, // a real leap day
	} {
		got := NormalizeEvent(ExtractedEvent{Date: tc.date, DatePrecision: tc.precision})
		if got.Date != tc.date || got.DatePrecision != tc.precision {
			t.Errorf("NormalizeEvent(%q,%q) = (%q,%q), want it left alone",
				tc.date, tc.precision, got.Date, got.DatePrecision)
		}
	}
}

// A date that does not match the precision the model claimed is not usable
// either: "2019" at "exact" precision means the model did not actually know
// the day.
func TestNormalizeEventRejectsDatePrecisionMismatch(t *testing.T) {
	got := NormalizeEvent(ExtractedEvent{Date: "2019", DatePrecision: "exact"})
	if got.Date != "" || got.DatePrecision != "unknown" {
		t.Fatalf("got (%q,%q), want the value cleared", got.Date, got.DatePrecision)
	}
}

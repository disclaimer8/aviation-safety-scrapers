package wayback

import "testing"

func intp(n int) *int { return &n }

func TestHasCriticalFields(t *testing.T) {
	ok := ExtractedEvent{Date: "2019-03-10", DatePrecision: "exact", AircraftType: "B738"}
	if !HasCriticalFields(ok) {
		t.Fatal("expected critical fields present")
	}
	noDate := ExtractedEvent{AircraftType: "B738"}
	if HasCriticalFields(noDate) {
		t.Fatal("missing date should fail gate")
	}
	noCraft := ExtractedEvent{Date: "2019", DatePrecision: "year"} // year precision is not usable
	if HasCriticalFields(noCraft) {
		t.Fatal("year precision + no aircraft should fail gate")
	}
}

func TestConfidenceScore(t *testing.T) {
	full := ExtractedEvent{
		Date: "2019-03-10", DatePrecision: "exact", Location: "Bishoftu",
		AircraftType: "B738", Fatalities: intp(157),
	}
	// 4/4 critical => base 80, +20 official => 100
	if got := ConfidenceScore(full, true); got != 100 {
		t.Fatalf("full official = %d, want 100", got)
	}
	// 4/4 critical, not official => 80
	if got := ConfidenceScore(full, false); got != 80 {
		t.Fatalf("full unofficial = %d, want 80", got)
	}
	// 2/4 (date + aircraft), official => round(0.5*80)=40 +20 = 60
	half := ExtractedEvent{Date: "2019-03-10", DatePrecision: "exact", AircraftType: "B738"}
	if got := ConfidenceScore(half, true); got != 60 {
		t.Fatalf("half official = %d, want 60", got)
	}
}

func TestNormalizeEvent(t *testing.T) {
	e := NormalizeEvent(ExtractedEvent{EventType: "crash", InvestigationStatus: "", ReportType: "weird", DatePrecision: ""})
	if e.EventType != "unknown" {
		t.Fatalf("event_type=%q want unknown", e.EventType)
	}
	if e.InvestigationStatus != "unknown" {
		t.Fatalf("investigation_status=%q want unknown", e.InvestigationStatus)
	}
	if e.ReportType != "final" {
		t.Fatalf("report_type=%q want final (default)", e.ReportType)
	}
	if e.DatePrecision != "unknown" {
		t.Fatalf("date_precision=%q want unknown", e.DatePrecision)
	}
}

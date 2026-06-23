package wayback

import "math"

// HasCriticalFields is the accident-promotion gate: a usable date (exact or
// month precision) AND at least one of registration / aircraft type.
func HasCriticalFields(e ExtractedEvent) bool {
	usableDate := e.Date != "" && (e.DatePrecision == "exact" || e.DatePrecision == "month")
	hasCraft := e.AircraftRegistration != "" || e.AircraftType != ""
	return usableDate && hasCraft
}

// ConfidenceScore is deterministic: the fraction of four critical fields present,
// scaled to 80, plus a 20-point bonus when the source is an official AAI. Capped
// at 100.
func ConfidenceScore(e ExtractedEvent, official bool) int {
	critical := []bool{
		e.Date != "" && (e.DatePrecision == "exact" || e.DatePrecision == "month"),
		e.Location != "",
		e.AircraftType != "" || e.AircraftRegistration != "",
		e.Fatalities != nil,
	}
	n := 0
	for _, ok := range critical {
		if ok {
			n++
		}
	}
	base := int(math.Round(float64(n) / 4.0 * 80.0))
	if official {
		base += 20
	}
	if base > 100 {
		base = 100
	}
	return base
}

func normalizeEnum(val string, allowed []string, def string) string {
	for _, a := range allowed {
		if val == a {
			return val
		}
	}
	return def
}

// NormalizeEvent clamps enum-valued fields to the DB's allowed sets so a write
// never violates a CHECK constraint.
func NormalizeEvent(e ExtractedEvent) ExtractedEvent {
	e.EventType = normalizeEnum(e.EventType,
		[]string{"accident", "serious_incident", "incident", "hijacking", "unknown"}, "unknown")
	e.InvestigationStatus = normalizeEnum(e.InvestigationStatus,
		[]string{"final_report_available", "preliminary_report_available",
			"investigation_open", "no_report_found", "unknown"}, "unknown")
	e.ReportType = normalizeEnum(e.ReportType,
		[]string{"final", "preliminary", "interim", "factual"}, "final")
	e.DatePrecision = normalizeEnum(e.DatePrecision,
		[]string{"exact", "month", "year", "unknown"}, "unknown")
	return e
}

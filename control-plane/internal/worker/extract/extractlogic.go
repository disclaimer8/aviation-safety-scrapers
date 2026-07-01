package extract

import (
	"math"
	"strings"
)

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
	e.Country = normalizeISO2(e.Country)
	return e
}

// normalizeISO2 upper-cases and trims a country code, returning "" for
// anything that isn't exactly two letters after trimming — a model that
// returns a country name, a lower-cased code, or garbage should map to
// "unknown" (NULL at promote time) rather than a malformed value reaching a
// countries.iso2 lookup.
func normalizeISO2(s string) string {
	s = strings.ToUpper(strings.TrimSpace(s))
	if len(s) != 2 {
		return ""
	}
	for _, r := range s {
		if r < 'A' || r > 'Z' {
			return ""
		}
	}
	return s
}

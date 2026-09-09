package extract

import (
	"math"
	"strings"
	"time"
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
	e.Date, e.DatePrecision = normalizeDate(e.Date, e.DatePrecision)
	e.Country = normalizeISO2(e.Country)
	return e
}

// normalizeDate rejects anything that is not an ISO-8601 date at the precision
// the model claimed, and checks the calendar.
//
// Nothing validated this before: "yesterday", "circa 1998" or "2019-13-45" all
// satisfied HasCriticalFields, promoted into events.date, and then keyed
// dedup — which links globally on (date, registration). A date the model
// invented is worse than no date, so an unparseable one is cleared and the
// precision drops to "unknown", which HasCriticalFields already rejects.
func normalizeDate(date, precision string) (string, string) {
	date = strings.TrimSpace(date)
	if date == "" {
		return "", precision
	}
	layouts := map[string]string{"exact": "2006-01-02", "month": "2006-01", "year": "2006"}
	layout, ok := layouts[precision]
	if !ok {
		// precision "unknown" — accept the value only if it is a well-formed
		// date at some precision, so nothing else downstream sees free text.
		for _, l := range []string{"2006-01-02", "2006-01", "2006"} {
			if _, err := time.Parse(l, date); err == nil {
				return date, precision
			}
		}
		return "", "unknown"
	}
	t, err := time.Parse(layout, date)
	if err != nil {
		return "", "unknown"
	}
	// time.Parse accepts a year far outside anything aviation could mean.
	if y := t.Year(); y < 1900 || y > time.Now().UTC().Year()+1 {
		return "", "unknown"
	}
	return date, precision
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

package foreignsearch

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"
)

// atsbInvestigation mirrors one item in the ATSB investigations JSON export.
// Field names match the schema used in testdata/atsb_export.json (schema-representative
// of the XHR response from atsb.gov.au investigations search; see task-6-report.md).
type atsbInvestigation struct {
	InvestigationNumber string `json:"InvestigationNumber"`
	Title               string `json:"Title"`
	OccurrenceDate      string `json:"OccurrenceDate"`
	InvestigationPageURL string `json:"InvestigationPageURL"`
	ReportPDFURL        string `json:"ReportPDFURL"`
}

// atsbExport is the top-level JSON structure: either a bare array or
// {"investigations":[...]}. We attempt both forms.
type atsbExport struct {
	Investigations []atsbInvestigation `json:"investigations"`
}

// parseATSB unmarshals an ATSB investigations JSON export and maps each item to a
// ForeignRecord. A non-JSON body returns an error. Rows missing InvestigationNumber
// are skipped and counted in warnings. The func is pure: no I/O, no side-effects.
func parseATSB(raw []byte) (recs []ForeignRecord, warnings int, err error) {
	if len(raw) == 0 {
		return nil, 0, errors.New("foreignsearch: atsb: empty input")
	}

	var items []atsbInvestigation

	// Try bare-array form first: [{"InvestigationNumber":...}, ...]
	trimmed := strings.TrimSpace(string(raw))
	if len(trimmed) > 0 && trimmed[0] == '[' {
		if jsonErr := json.Unmarshal(raw, &items); jsonErr != nil {
			return nil, 0, fmt.Errorf("foreignsearch: atsb: unmarshal array: %w", jsonErr)
		}
	} else {
		// Object form: {"investigations":[...]}
		var export atsbExport
		if jsonErr := json.Unmarshal(raw, &export); jsonErr != nil {
			return nil, 0, fmt.Errorf("foreignsearch: atsb: unmarshal object: %w", jsonErr)
		}
		items = export.Investigations
	}

	for _, item := range items {
		if item.InvestigationNumber == "" {
			warnings++
			continue
		}

		rec := ForeignRecord{
			ForeignRef:     item.InvestigationNumber,
			Title:          item.Title,
			OccurrenceDate: normalizeATSBDate(item.OccurrenceDate),
			OriginalURL:    item.InvestigationPageURL,
			ReportURL:      item.ReportPDFURL,
		}
		if rec.ReportURL != "" {
			rec.Mimetype = "application/pdf"
		}
		recs = append(recs, rec)
	}
	return recs, warnings, nil
}

// normalizeATSBDate returns the date in yyyy-mm-dd form when the input already
// matches that form, or passes it through otherwise. ATSB exports use ISO 8601
// dates (yyyy-mm-dd), so no reformatting is typically required.
func normalizeATSBDate(s string) string {
	s = strings.TrimSpace(s)
	if len(s) >= 10 && s[4] == '-' && s[7] == '-' {
		return s[:10]
	}
	return s
}

// atsbClient implements AuthorityClient by reading a pre-exported ATSB file.
// ATSB sits behind Akamai; the worker never fetches it live. An operator exports
// the investigations JSON from the mini-PC browser and passes the path via
// --source-file.
type atsbClient struct {
	sourceFile string
}

// NewATSBClient returns an AuthorityClient that reads investigations from sourceFile.
func NewATSBClient(sourceFile string) AuthorityClient {
	return &atsbClient{sourceFile: sourceFile}
}

// Search reads the source file and parses it. countryISO2 is accepted for interface
// compliance; the source file is assumed to already be filtered to the relevant country.
func (c *atsbClient) Search(_ context.Context, _ string) ([]ForeignRecord, error) {
	if c.sourceFile == "" {
		return nil, errors.New("foreignsearch: atsb requires --source-file")
	}
	raw, err := os.ReadFile(c.sourceFile)
	if err != nil {
		return nil, fmt.Errorf("foreignsearch: atsb: read source file: %w", err)
	}
	recs, _, err := parseATSB(raw)
	return recs, err
}

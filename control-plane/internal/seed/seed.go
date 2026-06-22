// Package seed populates the coverage control-plane database with the
// canonical ISO 3166-1 country list and curated policy/coverage overlays.
// All data is embedded at compile time; runtime seeding requires no network
// access. Apply is idempotent: repeated calls produce the same 249-row result.
package seed

import (
	"context"
	"database/sql"
	_ "embed"
	"encoding/json"
	"fmt"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/model"
)

// Stats reports how many rows were touched by a single Apply call.
// Fields for regional bodies, sources, and aircraft-origin routes are reserved
// for Task 5 and always return zero from this implementation.
type Stats struct {
	Countries            int
	RegionalBodies       int
	RegionalMembers      int
	Sources              int
	AircraftOriginRoutes int
}

//go:embed data/iso3166.json
var iso3166JSON []byte

//go:embed data/country_overlays.json
var overlaysJSON []byte

type isoEntry struct {
	ISO2   string `json:"iso2"`
	ISO3   string `json:"iso3"`
	Name   string `json:"name"`
	Region string `json:"region"`
}

type overlayEntry struct {
	ISO2                  string `json:"iso2"`
	Group                 string `json:"group"`
	PolicyStatus          string `json:"policy_status"`
	CoverageStatus        string `json:"coverage_status"`
	CoverageScore         int    `json:"coverage_score"`
	EffortScore           int    `json:"effort_score"`
	ExpectedRecords       int    `json:"expected_records"`
	ExpectedSourceQuality int    `json:"expected_source_quality"`
	RefreshCadence        string `json:"refresh_cadence"`
	Notes                 string `json:"notes"`
}

// Apply seeds the countries table from embedded ISO 3166-1 data and
// coverage/policy overlays. It is safe to call multiple times (idempotent
// UPSERT keyed on iso2). Returns Stats with Countries set to the number of
// rows upserted (always 249 on a valid run).
func Apply(ctx context.Context, db *sql.DB) (Stats, error) {
	countries, overlays, err := parseAndValidate()
	if err != nil {
		return Stats{}, err
	}

	overlayMap := make(map[string]overlayEntry, len(overlays))
	for _, o := range overlays {
		overlayMap[o.ISO2] = o
	}

	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return Stats{}, fmt.Errorf("seed: begin transaction: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	stmt, err := tx.PrepareContext(ctx, `
		INSERT INTO countries (
			iso2, iso3, name, region,
			policy_status, coverage_status,
			coverage_score, effort_score,
			expected_records, expected_source_quality,
			priority_score, country_group,
			refresh_cadence, notes
		) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(iso2) DO UPDATE SET
			iso3=excluded.iso3,
			name=excluded.name,
			region=excluded.region,
			policy_status=excluded.policy_status,
			coverage_status=excluded.coverage_status,
			coverage_score=excluded.coverage_score,
			effort_score=excluded.effort_score,
			expected_records=excluded.expected_records,
			expected_source_quality=excluded.expected_source_quality,
			priority_score=excluded.priority_score,
			country_group=excluded.country_group,
			refresh_cadence=excluded.refresh_cadence,
			notes=excluded.notes
	`)
	if err != nil {
		return Stats{}, fmt.Errorf("seed: prepare upsert: %w", err)
	}
	defer stmt.Close()

	for _, c := range countries {
		// Defaults for countries without an overlay.
		policyStatus := "allowed"
		coverageStatus := "unknown"
		coverageScore := 0
		effortScore := 3
		expectedRecords := 0
		expectedSourceQuality := 1
		var groupVal *string
		var refreshCadence *string
		var notes *string
		priority := model.PriorityScore(expectedRecords, expectedSourceQuality, effortScore)

		if o, ok := overlayMap[c.ISO2]; ok {
			policyStatus = o.PolicyStatus
			coverageStatus = o.CoverageStatus
			coverageScore = o.CoverageScore
			effortScore = o.EffortScore
			expectedRecords = o.ExpectedRecords
			expectedSourceQuality = o.ExpectedSourceQuality
			priority = model.PriorityScore(expectedRecords, expectedSourceQuality, effortScore)
			if o.Group != "" {
				g := o.Group
				groupVal = &g
			}
			if o.RefreshCadence != "" {
				r := o.RefreshCadence
				refreshCadence = &r
			}
			if o.Notes != "" {
				n := o.Notes
				notes = &n
			}
		}

		if _, err := stmt.ExecContext(ctx,
			c.ISO2, c.ISO3, c.Name, c.Region,
			policyStatus, coverageStatus,
			coverageScore, effortScore,
			expectedRecords, expectedSourceQuality,
			priority, groupVal,
			refreshCadence, notes,
		); err != nil {
			return Stats{}, fmt.Errorf("seed: upsert %s: %w", c.ISO2, err)
		}
	}

	if err := tx.Commit(); err != nil {
		return Stats{}, fmt.Errorf("seed: commit: %w", err)
	}

	return Stats{Countries: len(countries)}, nil
}

// parseAndValidate deserialises both embedded JSON files and runs structural
// validation before any database work begins. This ensures that a corrupted
// embed fails fast with a clear error rather than a mid-transaction abort.
func parseAndValidate() ([]isoEntry, []overlayEntry, error) {
	var countries []isoEntry
	if err := json.Unmarshal(iso3166JSON, &countries); err != nil {
		return nil, nil, fmt.Errorf("seed: parse iso3166.json: %w", err)
	}

	seen2 := make(map[string]bool, len(countries))
	seen3 := make(map[string]bool, len(countries))
	for _, c := range countries {
		if len(c.ISO2) != 2 {
			return nil, nil, fmt.Errorf("seed: iso3166 entry %q: iso2 must be length 2", c.ISO2)
		}
		if len(c.ISO3) != 3 {
			return nil, nil, fmt.Errorf("seed: iso3166 entry %q: iso3 must be length 3", c.ISO2)
		}
		if c.Name == "" {
			return nil, nil, fmt.Errorf("seed: iso3166 entry %q: name is empty", c.ISO2)
		}
		if c.Region == "" {
			return nil, nil, fmt.Errorf("seed: iso3166 entry %q: region is empty", c.ISO2)
		}
		if seen2[c.ISO2] {
			return nil, nil, fmt.Errorf("seed: iso3166 duplicate iso2 %q", c.ISO2)
		}
		if seen3[c.ISO3] {
			return nil, nil, fmt.Errorf("seed: iso3166 duplicate iso3 %q", c.ISO3)
		}
		seen2[c.ISO2] = true
		seen3[c.ISO3] = true
	}

	var overlays []overlayEntry
	if err := json.Unmarshal(overlaysJSON, &overlays); err != nil {
		return nil, nil, fmt.Errorf("seed: parse country_overlays.json: %w", err)
	}
	for _, o := range overlays {
		if !seen2[o.ISO2] {
			return nil, nil, fmt.Errorf("seed: overlay iso2 %q not in iso3166 list", o.ISO2)
		}
	}

	return countries, overlays, nil
}

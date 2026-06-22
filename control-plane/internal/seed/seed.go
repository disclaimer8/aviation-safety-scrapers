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

//go:embed data/regional_bodies.json
var regionalBodiesJSON []byte

//go:embed data/sources.json
var sourcesJSON []byte

//go:embed data/aircraft_origin_routes.json
var aircraftOriginRoutesJSON []byte

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

type regionalBodyMember struct {
	ISO2 string `json:"iso2"`
	Role string `json:"role"`
}

type regionalBodyEntry struct {
	Code       string               `json:"code"`
	Name       string               `json:"name"`
	BodyClass  string               `json:"body_class"`
	WebsiteURL string               `json:"website_url"`
	SourceURL  string               `json:"source_url"`
	Notes      string               `json:"notes"`
	Members    []regionalBodyMember `json:"members"`
}

type sourceEntry struct {
	Name                 string `json:"name"`
	URL                  string `json:"url"`
	CanonicalURL         string `json:"canonical_url"`
	SourceType           string `json:"source_type"`
	SourceTier           int    `json:"source_tier"`
	RobotsPolicy         string `json:"robots_policy"`
	CopyrightPolicyNotes string `json:"copyright_policy_notes"`
}

type aircraftOriginRouteEntry struct {
	Patterns           []string `json:"patterns"`
	Manufacturer       string   `json:"manufacturer"`
	StateOfDesignISO2  string   `json:"state_of_design_iso2"`
	StateOfMfgISO2     string   `json:"state_of_manufacture_iso2"`
	ExpectedSourceName string   `json:"expected_source_name"`
	Priority           int      `json:"priority"`
}

// Apply seeds the countries table from embedded ISO 3166-1 data and
// coverage/policy overlays, then seeds regional bodies + members, sources,
// and aircraft-origin routes — all within the SAME transaction. It is safe
// to call multiple times (idempotent UPSERTs keyed on unique constraints).
func Apply(ctx context.Context, db *sql.DB) (Stats, error) {
	countries, overlays, err := parseAndValidate()
	if err != nil {
		return Stats{}, err
	}

	var bodies []regionalBodyEntry
	if err := json.Unmarshal(regionalBodiesJSON, &bodies); err != nil {
		return Stats{}, fmt.Errorf("seed: parse regional_bodies.json: %w", err)
	}

	var sources []sourceEntry
	if err := json.Unmarshal(sourcesJSON, &sources); err != nil {
		return Stats{}, fmt.Errorf("seed: parse sources.json: %w", err)
	}

	var routes []aircraftOriginRouteEntry
	if err := json.Unmarshal(aircraftOriginRoutesJSON, &routes); err != nil {
		return Stats{}, fmt.Errorf("seed: parse aircraft_origin_routes.json: %w", err)
	}

	// Validate source tiers before touching the DB.
	for _, s := range sources {
		styp := model.SourceType(s.SourceType)
		if !model.SourceTierAllowsType(s.SourceTier, styp) {
			return Stats{}, fmt.Errorf("seed: source %q tier %d does not allow type %q",
				s.Name, s.SourceTier, s.SourceType)
		}
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

	// ── countries ────────────────────────────────────────────────────────────

	stmtCountry, err := tx.PrepareContext(ctx, `
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
		return Stats{}, fmt.Errorf("seed: prepare country upsert: %w", err)
	}
	defer stmtCountry.Close()

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

		if _, err := stmtCountry.ExecContext(ctx,
			c.ISO2, c.ISO3, c.Name, c.Region,
			policyStatus, coverageStatus,
			coverageScore, effortScore,
			expectedRecords, expectedSourceQuality,
			priority, groupVal,
			refreshCadence, notes,
		); err != nil {
			return Stats{}, fmt.Errorf("seed: upsert country %s: %w", c.ISO2, err)
		}
	}

	// Build an in-transaction ISO2 → country_id lookup map.
	rows, err := tx.QueryContext(ctx, `SELECT id, iso2 FROM countries`)
	if err != nil {
		return Stats{}, fmt.Errorf("seed: query countries: %w", err)
	}
	countryByISO2 := make(map[string]int64)
	for rows.Next() {
		var id int64
		var iso2 string
		if err := rows.Scan(&id, &iso2); err != nil {
			rows.Close()
			return Stats{}, fmt.Errorf("seed: scan country row: %w", err)
		}
		countryByISO2[iso2] = id
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return Stats{}, fmt.Errorf("seed: iterate countries: %w", err)
	}

	// ── regional bodies ──────────────────────────────────────────────────────

	stmtBody, err := tx.PrepareContext(ctx, `
		INSERT INTO regional_bodies (code, name, body_class, website_url, source_url, notes)
		VALUES (?, ?, ?, ?, ?, ?)
		ON CONFLICT(code) DO UPDATE SET
			name=excluded.name,
			body_class=excluded.body_class,
			website_url=excluded.website_url,
			source_url=excluded.source_url,
			notes=excluded.notes
	`)
	if err != nil {
		return Stats{}, fmt.Errorf("seed: prepare regional_bodies upsert: %w", err)
	}
	defer stmtBody.Close()

	stmtMember, err := tx.PrepareContext(ctx, `
		INSERT INTO regional_body_members (regional_body_id, country_id, role, source_url)
		VALUES (?, ?, ?, ?)
		ON CONFLICT(regional_body_id, country_id, role) DO UPDATE SET
			source_url=excluded.source_url
	`)
	if err != nil {
		return Stats{}, fmt.Errorf("seed: prepare regional_body_members upsert: %w", err)
	}
	defer stmtMember.Close()

	bodiesUpserted := 0
	membersUpserted := 0

	for _, b := range bodies {
		// Validate member ISO2 references before writing.
		for _, m := range b.Members {
			if _, ok := countryByISO2[m.ISO2]; !ok {
				return Stats{}, fmt.Errorf("seed: regional body %q member iso2 %q not found in countries",
					b.Code, m.ISO2)
			}
		}

		var websiteURL *string
		if b.WebsiteURL != "" {
			u := b.WebsiteURL
			websiteURL = &u
		}
		var bodyNotes *string
		if b.Notes != "" {
			n := b.Notes
			bodyNotes = &n
		}

		if _, err := stmtBody.ExecContext(ctx,
			b.Code, b.Name, b.BodyClass, websiteURL, b.SourceURL, bodyNotes,
		); err != nil {
			return Stats{}, fmt.Errorf("seed: upsert regional body %s: %w", b.Code, err)
		}
		bodiesUpserted++

		// Retrieve the body's ID (may have been inserted or already existed).
		var bodyID int64
		if err := tx.QueryRowContext(ctx,
			`SELECT id FROM regional_bodies WHERE code=?`, b.Code,
		).Scan(&bodyID); err != nil {
			return Stats{}, fmt.Errorf("seed: lookup regional body %s id: %w", b.Code, err)
		}

		for _, m := range b.Members {
			cid := countryByISO2[m.ISO2]
			if _, err := stmtMember.ExecContext(ctx, bodyID, cid, m.Role, b.SourceURL); err != nil {
				return Stats{}, fmt.Errorf("seed: upsert member %s/%s: %w", b.Code, m.ISO2, err)
			}
			membersUpserted++
		}
	}

	// ── sources ──────────────────────────────────────────────────────────────

	stmtSource, err := tx.PrepareContext(ctx, `
		INSERT INTO sources (name, url, canonical_url, source_type, source_tier,
			robots_policy, copyright_policy_notes)
		VALUES (?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(canonical_url, source_type) DO UPDATE SET
			name=excluded.name,
			url=excluded.url,
			source_tier=excluded.source_tier,
			robots_policy=excluded.robots_policy,
			copyright_policy_notes=excluded.copyright_policy_notes
	`)
	if err != nil {
		return Stats{}, fmt.Errorf("seed: prepare sources upsert: %w", err)
	}
	defer stmtSource.Close()

	sourcesUpserted := 0
	for _, s := range sources {
		var robotsPolicy *string
		if s.RobotsPolicy != "" {
			r := s.RobotsPolicy
			robotsPolicy = &r
		}
		var copyrightNotes *string
		if s.CopyrightPolicyNotes != "" {
			c := s.CopyrightPolicyNotes
			copyrightNotes = &c
		}
		if _, err := stmtSource.ExecContext(ctx,
			s.Name, s.URL, s.CanonicalURL, s.SourceType, s.SourceTier,
			robotsPolicy, copyrightNotes,
		); err != nil {
			return Stats{}, fmt.Errorf("seed: upsert source %q: %w", s.Name, err)
		}
		sourcesUpserted++
	}

	// ── aircraft origin routes ────────────────────────────────────────────────

	stmtRoute, err := tx.PrepareContext(ctx, `
		INSERT INTO aircraft_origin_routes (
			aircraft_type_pattern, normalized_pattern, manufacturer,
			state_of_design_country_id, state_of_manufacture_country_id,
			expected_authority_id, expected_source_name, priority
		) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
		ON CONFLICT(normalized_pattern, expected_source_name) DO UPDATE SET
			aircraft_type_pattern=excluded.aircraft_type_pattern,
			manufacturer=excluded.manufacturer,
			state_of_design_country_id=excluded.state_of_design_country_id,
			state_of_manufacture_country_id=excluded.state_of_manufacture_country_id,
			priority=excluded.priority
	`)
	if err != nil {
		return Stats{}, fmt.Errorf("seed: prepare aircraft_origin_routes upsert: %w", err)
	}
	defer stmtRoute.Close()

	routesUpserted := 0
	for _, r := range routes {
		designID, ok := countryByISO2[r.StateOfDesignISO2]
		if !ok {
			return Stats{}, fmt.Errorf("seed: aircraft route state_of_design iso2 %q not found",
				r.StateOfDesignISO2)
		}
		var mfgID *int64
		if r.StateOfMfgISO2 != "" {
			id, ok := countryByISO2[r.StateOfMfgISO2]
			if !ok {
				return Stats{}, fmt.Errorf("seed: aircraft route state_of_manufacture iso2 %q not found",
					r.StateOfMfgISO2)
			}
			mfgID = &id
		}

		for _, pattern := range r.Patterns {
			normalized := model.NormalizeName(pattern)
			if _, err := stmtRoute.ExecContext(ctx,
				pattern, normalized, r.Manufacturer,
				designID, mfgID,
				r.ExpectedSourceName, r.Priority,
			); err != nil {
				return Stats{}, fmt.Errorf("seed: upsert aircraft route %q: %w", pattern, err)
			}
			routesUpserted++
		}
	}

	if err := tx.Commit(); err != nil {
		return Stats{}, fmt.Errorf("seed: commit: %w", err)
	}

	return Stats{
		Countries:            len(countries),
		RegionalBodies:       bodiesUpserted,
		RegionalMembers:      membersUpserted,
		Sources:              sourcesUpserted,
		AircraftOriginRoutes: routesUpserted,
	}, nil
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

	if len(countries) != 249 {
		return nil, nil, fmt.Errorf("seed: iso3166.json contains %d entries, expected exactly 249", len(countries))
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

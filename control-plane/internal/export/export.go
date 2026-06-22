// Package export builds a deterministic, leakage-free JSON snapshot of the
// coverage control-plane canonical tables. The exported Document is sorted
// consistently so that successive calls with the same generatedAt produce
// byte-identical JSON. No staging tables, raw snapshots, raw contact blocks,
// private notes, or internal IDs are included.
package export

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"time"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/atomicfile"
)

// Document is the top-level export shape (spec §13).
type Document struct {
	SchemaVersion        int                   `json:"schema_version"`
	GeneratedAt          string                `json:"generated_at"`
	Countries            []Country             `json:"countries"`
	Authorities          []Authority           `json:"authorities"`
	RegionalBodies       []RegionalBody        `json:"regional_bodies"`
	RegionalBodyMembers  []RegionalBodyMember  `json:"regional_body_members"`
	Sources              []Source              `json:"sources"`
	AircraftOriginRoutes []AircraftOriginRoute `json:"aircraft_origin_routes"`
}

// Country mirrors the countries table fields that are safe for public export.
// Private operational notes are excluded.
type Country struct {
	ISO2                  string  `json:"iso2"`
	ISO3                  string  `json:"iso3"`
	Name                  string  `json:"name"`
	Region                string  `json:"region"`
	PolicyStatus          string  `json:"policy_status"`
	CoverageStatus        string  `json:"coverage_status"`
	CoverageScore         int     `json:"coverage_score"`
	EffortScore           int     `json:"effort_score"`
	ExpectedRecords       int     `json:"expected_records"`
	ExpectedSourceQuality int     `json:"expected_source_quality"`
	PriorityScore         float64 `json:"priority_score"`
	CountryGroup          *string `json:"country_group,omitempty"`
	RefreshCadence        *string `json:"refresh_cadence,omitempty"`
}

// Authority mirrors the public fields of the authorities table. Per-field
// provenance labels are included in Provenance; raw snapshots/overrides and
// private notes are excluded.
type Authority struct {
	CountryISO2      string  `json:"country_iso2"`
	NormalizedName   string  `json:"normalized_name"`
	Name             string  `json:"name"`
	Type             string  `json:"type"`
	WebsiteURL       *string `json:"website_url,omitempty"`
	ArchiveURL       *string `json:"archive_url,omitempty"`
	ContactEmail     *string `json:"contact_email,omitempty"`
	ContactPhone     *string `json:"contact_phone,omitempty"`
	SourceURL        string  `json:"source_url"`
	SourceName       string  `json:"source_name"`
	HasPublicArchive *int    `json:"has_public_archive,omitempty"`
	Status           string  `json:"status"`
	// Provenance maps each contact field name to its provenance_kind label
	// (seed / icao_snapshot / curated_override). Only fields that have a
	// provenance row appear in the map. The four contact fields are:
	// website_url, archive_url, contact_email, contact_phone.
	Provenance map[string]string `json:"provenance,omitempty"`
}

// RegionalBody mirrors the regional_bodies table (notes excluded).
type RegionalBody struct {
	Code       string  `json:"code"`
	Name       string  `json:"name"`
	BodyClass  string  `json:"body_class"`
	WebsiteURL *string `json:"website_url,omitempty"`
	SourceURL  string  `json:"source_url"`
}

// RegionalBodyMember mirrors the regional_body_members table, using human-readable
// body code and country ISO2 instead of opaque integer IDs.
type RegionalBodyMember struct {
	BodyCode    string `json:"body_code"`
	CountryISO2 string `json:"country_iso2"`
	Role        string `json:"role"`
	SourceURL   string `json:"source_url"`
}

// Source mirrors the sources table fields safe for public export.
type Source struct {
	Name                 string  `json:"name"`
	URL                  string  `json:"url"`
	CanonicalURL         string  `json:"canonical_url"`
	SourceType           string  `json:"source_type"`
	SourceTier           int     `json:"source_tier"`
	RobotsPolicy         *string `json:"robots_policy,omitempty"`
	CopyrightPolicyNotes *string `json:"copyright_policy_notes,omitempty"`
	Active               int     `json:"active"`
	HealthStatus         string  `json:"health_status"`
}

// AircraftOriginRoute mirrors the aircraft_origin_routes table using
// human-readable ISO2 codes instead of internal IDs.
type AircraftOriginRoute struct {
	AircraftTypePattern    string  `json:"aircraft_type_pattern"`
	NormalizedPattern      string  `json:"normalized_pattern"`
	Manufacturer           string  `json:"manufacturer"`
	StateOfDesignISO2      string  `json:"state_of_design_iso2"`
	StateOfManufactureISO2 *string `json:"state_of_manufacture_iso2,omitempty"`
	ExpectedSourceName     string  `json:"expected_source_name"`
	Priority               int     `json:"priority"`
}

// Build assembles the Document from the canonical effective tables. Every SQL
// query has an explicit ORDER BY to ensure deterministic output. No staging
// tables, raw snapshots, raw contact blocks, or private notes are queried.
// All slice fields are guaranteed non-nil (empty slice rather than nil) so that
// JSON output always emits [] rather than null.
func Build(ctx context.Context, db *sql.DB, generatedAt time.Time) (Document, error) {
	doc := Document{
		SchemaVersion:        1,
		GeneratedAt:          generatedAt.UTC().Format(time.RFC3339),
		Countries:            []Country{},
		Authorities:          []Authority{},
		RegionalBodies:       []RegionalBody{},
		RegionalBodyMembers:  []RegionalBodyMember{},
		Sources:              []Source{},
		AircraftOriginRoutes: []AircraftOriginRoute{},
	}

	var err error

	doc.Countries, err = queryCountries(ctx, db)
	if err != nil {
		return Document{}, err
	}
	if doc.Countries == nil {
		doc.Countries = []Country{}
	}

	doc.Authorities, err = queryAuthorities(ctx, db)
	if err != nil {
		return Document{}, err
	}
	if doc.Authorities == nil {
		doc.Authorities = []Authority{}
	}

	doc.RegionalBodies, err = queryRegionalBodies(ctx, db)
	if err != nil {
		return Document{}, err
	}
	if doc.RegionalBodies == nil {
		doc.RegionalBodies = []RegionalBody{}
	}

	doc.RegionalBodyMembers, err = queryRegionalBodyMembers(ctx, db)
	if err != nil {
		return Document{}, err
	}
	if doc.RegionalBodyMembers == nil {
		doc.RegionalBodyMembers = []RegionalBodyMember{}
	}

	doc.Sources, err = querySources(ctx, db)
	if err != nil {
		return Document{}, err
	}
	if doc.Sources == nil {
		doc.Sources = []Source{}
	}

	doc.AircraftOriginRoutes, err = queryAircraftOriginRoutes(ctx, db)
	if err != nil {
		return Document{}, err
	}
	if doc.AircraftOriginRoutes == nil {
		doc.AircraftOriginRoutes = []AircraftOriginRoute{}
	}

	return doc, nil
}

// WriteJSON calls Build, marshals the Document with stable indentation, then
// atomically replaces output using atomicfile.Write.
func WriteJSON(ctx context.Context, db *sql.DB, output string, generatedAt time.Time) error {
	doc, err := Build(ctx, db, generatedAt)
	if err != nil {
		return fmt.Errorf("export: build: %w", err)
	}

	data, err := json.MarshalIndent(doc, "", "  ")
	if err != nil {
		return fmt.Errorf("export: marshal: %w", err)
	}

	if err := atomicfile.Write(output, data); err != nil {
		return fmt.Errorf("export: write: %w", err)
	}

	return nil
}

// queryCountries fetches countries ordered by iso2. Notes are excluded.
func queryCountries(ctx context.Context, db *sql.DB) ([]Country, error) {
	rows, err := db.QueryContext(ctx, `
		SELECT
			iso2, iso3, name, region,
			policy_status, coverage_status,
			coverage_score, effort_score,
			expected_records, expected_source_quality,
			priority_score, country_group, refresh_cadence
		FROM countries
		ORDER BY iso2
	`)
	if err != nil {
		return nil, fmt.Errorf("export: query countries: %w", err)
	}
	defer rows.Close()

	var out []Country
	for rows.Next() {
		var c Country
		if err := rows.Scan(
			&c.ISO2, &c.ISO3, &c.Name, &c.Region,
			&c.PolicyStatus, &c.CoverageStatus,
			&c.CoverageScore, &c.EffortScore,
			&c.ExpectedRecords, &c.ExpectedSourceQuality,
			&c.PriorityScore, &c.CountryGroup, &c.RefreshCadence,
		); err != nil {
			return nil, fmt.Errorf("export: scan country: %w", err)
		}
		out = append(out, c)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("export: iterate countries: %w", err)
	}
	return out, nil
}

// queryAuthorities fetches authorities with field-level provenance.
// Ordered by (country iso2, normalized_name, type). Notes and raw snapshot
// IDs are excluded — only the provenance_kind label per contact field is
// exported.
//
// To avoid a deadlock on the single-connection SQLite pool, we collect all
// authority rows (including internal IDs) in one pass, close the cursor, then
// query provenance for each authority in a second pass.
func queryAuthorities(ctx context.Context, db *sql.DB) ([]Authority, error) {
	rows, err := db.QueryContext(ctx, `
		SELECT
			c.iso2,
			a.normalized_name,
			a.name,
			a.type,
			a.website_url,
			a.archive_url,
			a.contact_email,
			a.contact_phone,
			a.source_url,
			a.source_name,
			a.has_public_archive,
			a.status,
			a.id
		FROM authorities a
		JOIN countries c ON c.id = a.country_id
		ORDER BY c.iso2, a.normalized_name, a.type
	`)
	if err != nil {
		return nil, fmt.Errorf("export: query authorities: %w", err)
	}

	type authorityWithID struct {
		auth Authority
		id   int64
	}
	var collected []authorityWithID
	for rows.Next() {
		var a Authority
		var id int64
		if err := rows.Scan(
			&a.CountryISO2,
			&a.NormalizedName,
			&a.Name,
			&a.Type,
			&a.WebsiteURL,
			&a.ArchiveURL,
			&a.ContactEmail,
			&a.ContactPhone,
			&a.SourceURL,
			&a.SourceName,
			&a.HasPublicArchive,
			&a.Status,
			&id,
		); err != nil {
			rows.Close()
			return nil, fmt.Errorf("export: scan authority: %w", err)
		}
		collected = append(collected, authorityWithID{auth: a, id: id})
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return nil, fmt.Errorf("export: iterate authorities: %w", err)
	}
	rows.Close()

	// Second pass: attach provenance labels. The rows cursor is closed, so we
	// can safely reuse the same single DB connection.
	out := make([]Authority, 0, len(collected))
	for _, item := range collected {
		prov, err := queryAuthorityProvenance(ctx, db, item.id)
		if err != nil {
			return nil, err
		}
		if len(prov) > 0 {
			item.auth.Provenance = prov
		}
		out = append(out, item.auth)
	}
	return out, nil
}

// queryAuthorityProvenance returns a map of field_name → provenance_kind for
// the four contact fields of the given authority. snapshot_id, override_id,
// effective_value, and updated_at are deliberately not exported.
func queryAuthorityProvenance(ctx context.Context, db *sql.DB, authorityID int64) (map[string]string, error) {
	rows, err := db.QueryContext(ctx, `
		SELECT field_name, provenance_kind
		FROM authority_field_provenance
		WHERE authority_id = ?
		  AND field_name IN ('website_url','archive_url','contact_email','contact_phone')
		ORDER BY field_name
	`, authorityID)
	if err != nil {
		return nil, fmt.Errorf("export: query authority provenance: %w", err)
	}
	defer rows.Close()

	var out map[string]string
	for rows.Next() {
		var field, kind string
		if err := rows.Scan(&field, &kind); err != nil {
			return nil, fmt.Errorf("export: scan authority provenance: %w", err)
		}
		if out == nil {
			out = make(map[string]string)
		}
		out[field] = kind
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("export: iterate authority provenance: %w", err)
	}
	return out, nil
}

// queryRegionalBodies fetches regional_bodies ordered by code. Notes excluded.
func queryRegionalBodies(ctx context.Context, db *sql.DB) ([]RegionalBody, error) {
	rows, err := db.QueryContext(ctx, `
		SELECT code, name, body_class, website_url, source_url
		FROM regional_bodies
		ORDER BY code
	`)
	if err != nil {
		return nil, fmt.Errorf("export: query regional_bodies: %w", err)
	}
	defer rows.Close()

	var out []RegionalBody
	for rows.Next() {
		var b RegionalBody
		if err := rows.Scan(&b.Code, &b.Name, &b.BodyClass, &b.WebsiteURL, &b.SourceURL); err != nil {
			return nil, fmt.Errorf("export: scan regional_body: %w", err)
		}
		out = append(out, b)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("export: iterate regional_bodies: %w", err)
	}
	return out, nil
}

// queryRegionalBodyMembers fetches members ordered by (body code, country iso2, role).
func queryRegionalBodyMembers(ctx context.Context, db *sql.DB) ([]RegionalBodyMember, error) {
	rows, err := db.QueryContext(ctx, `
		SELECT
			rb.code,
			c.iso2,
			m.role,
			m.source_url
		FROM regional_body_members m
		JOIN regional_bodies rb ON rb.id = m.regional_body_id
		JOIN countries c ON c.id = m.country_id
		ORDER BY rb.code, c.iso2, m.role
	`)
	if err != nil {
		return nil, fmt.Errorf("export: query regional_body_members: %w", err)
	}
	defer rows.Close()

	var out []RegionalBodyMember
	for rows.Next() {
		var m RegionalBodyMember
		if err := rows.Scan(&m.BodyCode, &m.CountryISO2, &m.Role, &m.SourceURL); err != nil {
			return nil, fmt.Errorf("export: scan regional_body_member: %w", err)
		}
		out = append(out, m)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("export: iterate regional_body_members: %w", err)
	}
	return out, nil
}

// querySources fetches sources ordered by (canonical_url, source_type).
func querySources(ctx context.Context, db *sql.DB) ([]Source, error) {
	rows, err := db.QueryContext(ctx, `
		SELECT
			name, url, canonical_url, source_type, source_tier,
			robots_policy, copyright_policy_notes,
			active, health_status
		FROM sources
		ORDER BY canonical_url, source_type
	`)
	if err != nil {
		return nil, fmt.Errorf("export: query sources: %w", err)
	}
	defer rows.Close()

	var out []Source
	for rows.Next() {
		var s Source
		if err := rows.Scan(
			&s.Name, &s.URL, &s.CanonicalURL, &s.SourceType, &s.SourceTier,
			&s.RobotsPolicy, &s.CopyrightPolicyNotes,
			&s.Active, &s.HealthStatus,
		); err != nil {
			return nil, fmt.Errorf("export: scan source: %w", err)
		}
		out = append(out, s)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("export: iterate sources: %w", err)
	}
	return out, nil
}

// queryAircraftOriginRoutes fetches aircraft origin routes ordered by
// (normalized_pattern, expected_source_name). Internal IDs are resolved to
// human-readable ISO2 codes.
func queryAircraftOriginRoutes(ctx context.Context, db *sql.DB) ([]AircraftOriginRoute, error) {
	rows, err := db.QueryContext(ctx, `
		SELECT
			aor.aircraft_type_pattern,
			aor.normalized_pattern,
			aor.manufacturer,
			cd.iso2,
			cm.iso2,
			aor.expected_source_name,
			aor.priority
		FROM aircraft_origin_routes aor
		JOIN countries cd ON cd.id = aor.state_of_design_country_id
		LEFT JOIN countries cm ON cm.id = aor.state_of_manufacture_country_id
		ORDER BY aor.normalized_pattern, aor.expected_source_name
	`)
	if err != nil {
		return nil, fmt.Errorf("export: query aircraft_origin_routes: %w", err)
	}
	defer rows.Close()

	var out []AircraftOriginRoute
	for rows.Next() {
		var r AircraftOriginRoute
		if err := rows.Scan(
			&r.AircraftTypePattern,
			&r.NormalizedPattern,
			&r.Manufacturer,
			&r.StateOfDesignISO2,
			&r.StateOfManufactureISO2,
			&r.ExpectedSourceName,
			&r.Priority,
		); err != nil {
			return nil, fmt.Errorf("export: scan aircraft_origin_route: %w", err)
		}
		out = append(out, r)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("export: iterate aircraft_origin_routes: %w", err)
	}
	return out, nil
}

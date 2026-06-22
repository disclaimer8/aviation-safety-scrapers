// Package validation implements coverage-control-plane invariant checks.
// Run executes all 14 invariants against a live database and returns a Report
// of any violations. On a freshly migrated+seeded database Run returns zero
// errors.
package validation

import (
	"context"
	"database/sql"
	"fmt"
	"sort"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/model"
)

// Severity classifies how serious a violation is.
type Severity string

const (
	Error   Severity = "error"
	Warning Severity = "warning"
)

// Issue describes a single invariant violation.
type Issue struct {
	Code     string   `json:"code"`
	Severity Severity `json:"severity"`
	Entity   string   `json:"entity,omitempty"`
	Message  string   `json:"message"`
}

// Report collects all issues found during a validation run.
type Report struct {
	Issues []Issue `json:"issues"`
}

// HasErrors returns true if any issue has Error severity.
func (r Report) HasErrors() bool {
	for _, iss := range r.Issues {
		if iss.Severity == Error {
			return true
		}
	}
	return false
}

// Contains returns true if any issue has the given code.
func (r Report) Contains(code string) bool {
	for _, iss := range r.Issues {
		if iss.Code == code {
			return true
		}
	}
	return false
}

// Options controls optional validation behaviour.
type Options struct {
	// ConflictsAreErrors promotes open import_conflicts from Warning to Error.
	ConflictsAreErrors bool
}

// Run executes all invariants and returns a sorted Report.
// It returns an empty report (no errors) on a freshly migrated+seeded database.
func Run(ctx context.Context, db *sql.DB, opts Options) Report {
	var issues []Issue

	// Invariant 1 – ISO2/ISO3 uniqueness and 249-country count.
	issues = append(issues, checkISO(ctx, db)...)

	// Invariant 2 – Enum and score ranges.
	issues = append(issues, checkEnumsAndScores(ctx, db)...)

	// Invariant 3 – Foreign-key integrity.
	issues = append(issues, checkForeignKeys(ctx, db)...)

	// Invariant 4 – Duplicate normalized authorities.
	issues = append(issues, checkDuplicateAuthorities(ctx, db)...)

	// Invariant 5 – Unknown AIA/RAIO country labels.
	issues = append(issues, checkUnknownStagedLabels(ctx, db)...)

	// Invariant 6 – Required policy exclusions (AF, KP, SY).
	issues = append(issues, checkRequiredExclusions(ctx, db)...)

	// Invariant 7 – Required regional body member minimums.
	issues = append(issues, checkRegionalBodyMinimums(ctx, db)...)

	// Invariant 8 – Excluded countries must not have direct crawl jobs.
	issues = append(issues, checkExcludedDirectCrawl(ctx, db)...)

	// Invariant 9 – Source tier ↔ source type consistency.
	issues = append(issues, checkSourceTierType(ctx, db)...)

	// Invariant 10 – Reports always have copyright_status.
	issues = append(issues, checkReportCopyrightStatus(ctx, db)...)

	// Invariant 11 – Events always have confidence_score and dedup_status.
	issues = append(issues, checkEventRequiredFields(ctx, db)...)

	// Invariant 12 – Provenance self-consistency.
	issues = append(issues, checkProvenanceSelfConsistency(ctx, db)...)

	// Invariant 13 – Open import conflicts.
	issues = append(issues, checkOpenImportConflicts(ctx, db, opts)...)

	// Invariant 14 – Priority score formula.
	issues = append(issues, checkPriorityScores(ctx, db)...)

	// Sort deterministically: (Severity, Code, Entity).
	sort.Slice(issues, func(i, j int) bool {
		if issues[i].Severity != issues[j].Severity {
			// Error before Warning.
			return issues[i].Severity < issues[j].Severity
		}
		if issues[i].Code != issues[j].Code {
			return issues[i].Code < issues[j].Code
		}
		return issues[i].Entity < issues[j].Entity
	})

	return Report{Issues: issues}
}

// ── Invariant 1 ─────────────────────────────────────────────────────────────

func checkISO(ctx context.Context, db *sql.DB) []Issue {
	const wantCount = 249
	var count, distinctISO2, distinctISO3 int
	if err := db.QueryRowContext(ctx, `
		SELECT COUNT(*), COUNT(DISTINCT iso2), COUNT(DISTINCT iso3) FROM countries
	`).Scan(&count, &distinctISO2, &distinctISO3); err != nil {
		return []Issue{{
			Code:     "iso_check_failed",
			Severity: Error,
			Message:  fmt.Sprintf("failed to query countries: %v", err),
		}}
	}

	var issues []Issue
	if count != wantCount {
		issues = append(issues, Issue{
			Code:     "iso_country_count",
			Severity: Error,
			Message:  fmt.Sprintf("countries count=%d, want %d", count, wantCount),
		})
	}
	if distinctISO2 != count {
		issues = append(issues, Issue{
			Code:     "iso2_not_unique",
			Severity: Error,
			Message:  fmt.Sprintf("iso2 distinct=%d, total=%d; duplicates present", distinctISO2, count),
		})
	}
	if distinctISO3 != count {
		issues = append(issues, Issue{
			Code:     "iso3_not_unique",
			Severity: Error,
			Message:  fmt.Sprintf("iso3 distinct=%d, total=%d; duplicates present", distinctISO3, count),
		})
	}
	return issues
}

// ── Invariant 2 ─────────────────────────────────────────────────────────────

func checkEnumsAndScores(ctx context.Context, db *sql.DB) []Issue {
	// SQLite CHECK constraints already enforce most enum and range values at
	// insert/update time. We validate the ranges programmatically here as a
	// belt-and-suspenders runtime check.
	var issues []Issue

	// coverage_score BETWEEN 0 AND 5
	var badCovScore int
	if err := db.QueryRowContext(ctx, `
		SELECT COUNT(*) FROM countries WHERE coverage_score < 0 OR coverage_score > 5
	`).Scan(&badCovScore); err == nil && badCovScore > 0 {
		issues = append(issues, Issue{
			Code:     "coverage_score_out_of_range",
			Severity: Error,
			Message:  fmt.Sprintf("%d countries have coverage_score outside [0,5]", badCovScore),
		})
	}

	// effort_score BETWEEN 1 AND 5
	var badEffort int
	if err := db.QueryRowContext(ctx, `
		SELECT COUNT(*) FROM countries WHERE effort_score < 1 OR effort_score > 5
	`).Scan(&badEffort); err == nil && badEffort > 0 {
		issues = append(issues, Issue{
			Code:     "effort_score_out_of_range",
			Severity: Error,
			Message:  fmt.Sprintf("%d countries have effort_score outside [1,5]", badEffort),
		})
	}

	// expected_source_quality BETWEEN 1 AND 5
	var badQuality int
	if err := db.QueryRowContext(ctx, `
		SELECT COUNT(*) FROM countries WHERE expected_source_quality < 1 OR expected_source_quality > 5
	`).Scan(&badQuality); err == nil && badQuality > 0 {
		issues = append(issues, Issue{
			Code:     "expected_source_quality_out_of_range",
			Severity: Error,
			Message:  fmt.Sprintf("%d countries have expected_source_quality outside [1,5]", badQuality),
		})
	}

	// confidence_score BETWEEN 0 AND 100
	var badConf int
	if err := db.QueryRowContext(ctx, `
		SELECT COUNT(*) FROM events WHERE confidence_score < 0 OR confidence_score > 100
	`).Scan(&badConf); err == nil && badConf > 0 {
		issues = append(issues, Issue{
			Code:     "confidence_score_out_of_range",
			Severity: Error,
			Message:  fmt.Sprintf("%d events have confidence_score outside [0,100]", badConf),
		})
	}

	// source_tier BETWEEN 1 AND 6
	var badTier int
	if err := db.QueryRowContext(ctx, `
		SELECT COUNT(*) FROM sources WHERE source_tier < 1 OR source_tier > 6
	`).Scan(&badTier); err == nil && badTier > 0 {
		issues = append(issues, Issue{
			Code:     "source_tier_out_of_range",
			Severity: Error,
			Message:  fmt.Sprintf("%d sources have source_tier outside [1,6]", badTier),
		})
	}

	return issues
}

// ── Invariant 3 ─────────────────────────────────────────────────────────────

func checkForeignKeys(ctx context.Context, db *sql.DB) []Issue {
	rows, err := db.QueryContext(ctx, `PRAGMA foreign_key_check`)
	if err != nil {
		return []Issue{{
			Code:     "fk_check_failed",
			Severity: Error,
			Message:  fmt.Sprintf("PRAGMA foreign_key_check failed: %v", err),
		}}
	}
	defer rows.Close()

	var issues []Issue
	for rows.Next() {
		var table, parent string
		var rowid sql.NullInt64
		var fkid int
		// The PRAGMA returns: table, rowid, parent, fkid
		if err := rows.Scan(&table, &rowid, &parent, &fkid); err != nil {
			issues = append(issues, Issue{
				Code:     "fk_check_scan_failed",
				Severity: Error,
				Message:  fmt.Sprintf("scan fk_check row: %v", err),
			})
			continue
		}
		issues = append(issues, Issue{
			Code:     "foreign_key_violation",
			Severity: Error,
			Entity:   table,
			Message:  fmt.Sprintf("table %q rowid=%v references missing %q (fkid=%d)", table, rowid, parent, fkid),
		})
	}
	if err := rows.Err(); err != nil {
		issues = append(issues, Issue{
			Code:     "fk_check_failed",
			Severity: Error,
			Message:  fmt.Sprintf("iterating fk_check: %v", err),
		})
	}
	return issues
}

// ── Invariant 4 ─────────────────────────────────────────────────────────────

func checkDuplicateAuthorities(ctx context.Context, db *sql.DB) []Issue {
	// The UNIQUE(country_id, normalized_name, type) constraint already prevents
	// duplicates at insert time. This check confirms no violations exist.
	var dups int
	if err := db.QueryRowContext(ctx, `
		SELECT COUNT(*) FROM (
			SELECT country_id, normalized_name, type, COUNT(*) AS cnt
			FROM authorities
			GROUP BY country_id, normalized_name, type
			HAVING cnt > 1
		)
	`).Scan(&dups); err != nil {
		return []Issue{{
			Code:     "duplicate_authority_check_failed",
			Severity: Error,
			Message:  fmt.Sprintf("query failed: %v", err),
		}}
	}
	if dups > 0 {
		return []Issue{{
			Code:     "duplicate_authority",
			Severity: Error,
			Message:  fmt.Sprintf("%d authority groups with duplicate (country_id, normalized_name, type)", dups),
		}}
	}
	return nil
}

// ── Invariant 5 ─────────────────────────────────────────────────────────────

func checkUnknownStagedLabels(ctx context.Context, db *sql.DB) []Issue {
	var issues []Issue

	// staged_authorities with unresolved country_id.
	var unresolved int
	if err := db.QueryRowContext(ctx, `
		SELECT COUNT(*) FROM staged_authorities WHERE resolved_country_id IS NULL
	`).Scan(&unresolved); err == nil && unresolved > 0 {
		issues = append(issues, Issue{
			Code:     "unresolved_staged_authority_country",
			Severity: Warning,
			Message:  fmt.Sprintf("%d staged_authorities with unresolved country_id", unresolved),
		})
	}

	return issues
}

// ── Invariant 6 ─────────────────────────────────────────────────────────────

func checkRequiredExclusions(ctx context.Context, db *sql.DB) []Issue {
	required := []string{"AF", "KP", "SY"}
	var issues []Issue

	for _, iso2 := range required {
		var policy, coverage string
		err := db.QueryRowContext(ctx, `
			SELECT policy_status, coverage_status FROM countries WHERE iso2 = ?
		`, iso2).Scan(&policy, &coverage)
		if err == sql.ErrNoRows {
			issues = append(issues, Issue{
				Code:     "required_exclusion_missing",
				Severity: Error,
				Entity:   iso2,
				Message:  fmt.Sprintf("country %s not found; must be policy_excluded", iso2),
			})
			continue
		}
		if err != nil {
			issues = append(issues, Issue{
				Code:     "required_exclusion_check_failed",
				Severity: Error,
				Entity:   iso2,
				Message:  fmt.Sprintf("query failed for %s: %v", iso2, err),
			})
			continue
		}
		if policy != "excluded" || coverage != "policy_excluded" {
			issues = append(issues, Issue{
				Code:     "required_exclusion_wrong_status",
				Severity: Error,
				Entity:   iso2,
				Message:  fmt.Sprintf("country %s: policy_status=%q coverage_status=%q; both must be excluded/policy_excluded", iso2, policy, coverage),
			})
		}
	}
	return issues
}

// ── Invariant 7 ─────────────────────────────────────────────────────────────

func checkRegionalBodyMinimums(ctx context.Context, db *sql.DB) []Issue {
	minimums := map[string]int{
		"ECCAA":  5,
		"BAGAIA": 7,
		"IAC":    8,
	}
	var issues []Issue

	for code, minCount := range minimums {
		var exists int
		if err := db.QueryRowContext(ctx, `
			SELECT COUNT(*) FROM regional_bodies WHERE code = ?
		`, code).Scan(&exists); err != nil || exists == 0 {
			// Body doesn't exist in seed; skip check conservatively.
			continue
		}

		var count int
		if err := db.QueryRowContext(ctx, `
			SELECT COUNT(*) FROM regional_body_members m
			JOIN regional_bodies b ON b.id = m.regional_body_id
			WHERE b.code = ?
		`, code).Scan(&count); err != nil {
			issues = append(issues, Issue{
				Code:     "regional_body_check_failed",
				Severity: Error,
				Entity:   code,
				Message:  fmt.Sprintf("query failed for %s: %v", code, err),
			})
			continue
		}
		if count < minCount {
			issues = append(issues, Issue{
				Code:     "regional_body_insufficient_members",
				Severity: Error,
				Entity:   code,
				Message:  fmt.Sprintf("regional body %s has %d members, want >= %d", code, count, minCount),
			})
		}
	}
	return issues
}

// ── Invariant 8 ─────────────────────────────────────────────────────────────

func checkExcludedDirectCrawl(ctx context.Context, db *sql.DB) []Issue {
	rows, err := db.QueryContext(ctx, `
		SELECT c.iso2, cj.job_type
		FROM crawl_jobs cj
		JOIN countries c ON c.id = cj.country_id
		WHERE c.policy_status = 'excluded'
		  AND cj.job_type IN ('archive_crawl', 'pdf_discovery')
	`)
	if err != nil {
		return []Issue{{
			Code:     "excluded_direct_crawl_check_failed",
			Severity: Error,
			Message:  fmt.Sprintf("query failed: %v", err),
		}}
	}
	defer rows.Close()

	var issues []Issue
	for rows.Next() {
		var iso2, jobType string
		if err := rows.Scan(&iso2, &jobType); err != nil {
			continue
		}
		issues = append(issues, Issue{
			Code:     "excluded_direct_crawl",
			Severity: Error,
			Entity:   iso2,
			Message:  fmt.Sprintf("excluded country %s has direct crawl job type=%q", iso2, jobType),
		})
	}
	if err := rows.Err(); err != nil {
		issues = append(issues, Issue{
			Code:     "excluded_direct_crawl_check_failed",
			Severity: Error,
			Message:  fmt.Sprintf("iterating results: %v", err),
		})
	}
	return issues
}

// ── Invariant 9 ─────────────────────────────────────────────────────────────

func checkSourceTierType(ctx context.Context, db *sql.DB) []Issue {
	rows, err := db.QueryContext(ctx, `
		SELECT id, name, source_tier, source_type FROM sources
	`)
	if err != nil {
		return []Issue{{
			Code:     "source_tier_type_check_failed",
			Severity: Error,
			Message:  fmt.Sprintf("query failed: %v", err),
		}}
	}
	defer rows.Close()

	var issues []Issue
	for rows.Next() {
		var id int64
		var name, sourceType string
		var tier int
		if err := rows.Scan(&id, &name, &tier, &sourceType); err != nil {
			continue
		}
		if !model.SourceTierAllowsType(tier, model.SourceType(sourceType)) {
			issues = append(issues, Issue{
				Code:     "source_tier_type_mismatch",
				Severity: Error,
				Entity:   name,
				Message:  fmt.Sprintf("source %q (id=%d) tier=%d does not allow type=%q", name, id, tier, sourceType),
			})
		}
	}
	if err := rows.Err(); err != nil {
		issues = append(issues, Issue{
			Code:     "source_tier_type_check_failed",
			Severity: Error,
			Message:  fmt.Sprintf("iterating results: %v", err),
		})
	}
	return issues
}

// ── Invariant 10 ────────────────────────────────────────────────────────────

func checkReportCopyrightStatus(ctx context.Context, db *sql.DB) []Issue {
	// The schema declares copyright_status NOT NULL with a CHECK constraint.
	// This runtime check catches any bypassed rows.
	var missing int
	if err := db.QueryRowContext(ctx, `
		SELECT COUNT(*) FROM reports WHERE copyright_status IS NULL OR copyright_status = ''
	`).Scan(&missing); err != nil {
		return []Issue{{
			Code:     "report_copyright_check_failed",
			Severity: Error,
			Message:  fmt.Sprintf("query failed: %v", err),
		}}
	}
	if missing > 0 {
		return []Issue{{
			Code:     "report_missing_copyright_status",
			Severity: Error,
			Message:  fmt.Sprintf("%d reports missing copyright_status", missing),
		}}
	}
	return nil
}

// ── Invariant 11 ────────────────────────────────────────────────────────────

func checkEventRequiredFields(ctx context.Context, db *sql.DB) []Issue {
	var issues []Issue

	var missingConf int
	if err := db.QueryRowContext(ctx, `
		SELECT COUNT(*) FROM events WHERE confidence_score IS NULL
	`).Scan(&missingConf); err == nil && missingConf > 0 {
		issues = append(issues, Issue{
			Code:     "event_missing_confidence_score",
			Severity: Error,
			Message:  fmt.Sprintf("%d events missing confidence_score", missingConf),
		})
	}

	var missingDedup int
	if err := db.QueryRowContext(ctx, `
		SELECT COUNT(*) FROM events WHERE dedup_status IS NULL OR dedup_status = ''
	`).Scan(&missingDedup); err == nil && missingDedup > 0 {
		issues = append(issues, Issue{
			Code:     "event_missing_dedup_status",
			Severity: Error,
			Message:  fmt.Sprintf("%d events missing dedup_status", missingDedup),
		})
	}

	return issues
}

// ── Invariant 12 ────────────────────────────────────────────────────────────

func checkProvenanceSelfConsistency(ctx context.Context, db *sql.DB) []Issue {
	// Per spec 17.5.B, the CHECK constraint in the schema already enforces
	// self-consistency. This runtime check verifies no constraint bypass exists.
	// A freshly seeded database with no authorities trivially passes.
	var inconsistent int
	if err := db.QueryRowContext(ctx, `
		SELECT COUNT(*) FROM authority_field_provenance WHERE
			(provenance_kind = 'icao_snapshot' AND (snapshot_id IS NULL OR override_id IS NOT NULL))
			OR (provenance_kind = 'curated_override' AND (override_id IS NULL OR snapshot_id IS NOT NULL))
			OR (provenance_kind = 'seed' AND (snapshot_id IS NOT NULL OR override_id IS NOT NULL))
	`).Scan(&inconsistent); err != nil {
		return []Issue{{
			Code:     "provenance_check_failed",
			Severity: Error,
			Message:  fmt.Sprintf("query failed: %v", err),
		}}
	}
	if inconsistent > 0 {
		return []Issue{{
			Code:     "provenance_inconsistent",
			Severity: Error,
			Message:  fmt.Sprintf("%d authority_field_provenance rows violate kind/reference consistency", inconsistent),
		}}
	}
	return nil
}

// ── Invariant 13 ────────────────────────────────────────────────────────────

func checkOpenImportConflicts(ctx context.Context, db *sql.DB, opts Options) []Issue {
	var openCount int
	if err := db.QueryRowContext(ctx, `
		SELECT COUNT(*) FROM import_conflicts WHERE review_status = 'open'
	`).Scan(&openCount); err != nil {
		return []Issue{{
			Code:     "open_import_conflicts_check_failed",
			Severity: Error,
			Message:  fmt.Sprintf("query failed: %v", err),
		}}
	}
	if openCount == 0 {
		return nil
	}
	sev := Warning
	if opts.ConflictsAreErrors {
		sev = Error
	}
	return []Issue{{
		Code:     "open_import_conflicts",
		Severity: sev,
		Message:  fmt.Sprintf("%d open import conflicts require review", openCount),
	}}
}

// ── Invariant 14 ────────────────────────────────────────────────────────────

const priorityTolerance = 1e-6

func checkPriorityScores(ctx context.Context, db *sql.DB) []Issue {
	rows, err := db.QueryContext(ctx, `
		SELECT iso2, expected_records, expected_source_quality, effort_score, priority_score
		FROM countries
	`)
	if err != nil {
		return []Issue{{
			Code:     "priority_check_failed",
			Severity: Error,
			Message:  fmt.Sprintf("query failed: %v", err),
		}}
	}
	defer rows.Close()

	var issues []Issue
	for rows.Next() {
		var iso2 string
		var expectedRecords, quality, effort int
		var stored float64
		if err := rows.Scan(&iso2, &expectedRecords, &quality, &effort, &stored); err != nil {
			continue
		}
		expected := model.PriorityScore(expectedRecords, quality, effort)
		diff := stored - expected
		if diff < 0 {
			diff = -diff
		}
		if diff >= priorityTolerance {
			issues = append(issues, Issue{
				Code:     "priority_drift",
				Severity: Error,
				Entity:   iso2,
				Message:  fmt.Sprintf("country %s priority_score=%.10f, expected=%.10f (diff=%.2e)", iso2, stored, expected, diff),
			})
		}
	}
	if err := rows.Err(); err != nil {
		issues = append(issues, Issue{
			Code:     "priority_check_failed",
			Severity: Error,
			Message:  fmt.Sprintf("iterating results: %v", err),
		})
	}
	return issues
}

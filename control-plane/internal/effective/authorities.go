// Package effective resolves the effective value of each mutable authority
// field from three precedence layers and records per-field provenance.
//
// Precedence (spec §7), highest first:
//  1. Active curated override (field_overrides). The override value always wins;
//     a differing incoming value records an open conflict but never overwrites.
//  2. Incoming ICAO snapshot value, when non-empty and no override exists.
//  3. Preserve the current stored value when the incoming value is empty but the
//     stored value is not (an upstream removal), recording a removal conflict.
//
// Every effective write upserts exactly one authority_field_provenance row per
// (authority_id, field_name) carrying the provenance kind and the matching
// snapshot_id / override_id, satisfying the table's self-consistency CHECK.
package effective

import (
	"context"
	"database/sql"
	"fmt"
	"strings"
)

// DBTX is satisfied by both *sql.DB and *sql.Tx so ApplyAuthority runs either on
// a plain database (tests) or inside the import transaction (Task 9). It mirrors
// provenance.DBTX intentionally to avoid a cross-package coupling.
type DBTX interface {
	ExecContext(context.Context, string, ...any) (sql.Result, error)
	QueryRowContext(context.Context, string, ...any) *sql.Row
}

// IncomingAuthority carries one parsed ICAO authority record plus the import
// context (run + snapshot) needed to attribute provenance and conflicts.
type IncomingAuthority struct {
	RunID          int64
	CountryID      int64
	Name           string
	NormalizedName string
	Type           string
	WebsiteURL     string
	ArchiveURL     string
	ContactEmail   string
	ContactPhone   string
	SourceURL      string
	SourceName     string
	SnapshotID     int64
}

// ApplyResult reports the outcome of one ApplyAuthority call.
type ApplyResult struct {
	AuthorityID int64
	Applied     int // fields whose effective value was (re)written
	Conflicts   int // conflicts created this call
}

// mutableField pairs an authority column with the incoming value for it. The
// column names are package-internal constants, never user input, so they are
// safe to interpolate into SQL.
type mutableField struct {
	column   string
	incoming string
}

// ApplyAuthority upserts the authority identified by (country_id,
// normalized_name, type) and resolves each mutable field per the package
// precedence rules, writing effective values, field provenance, and any
// conflicts within the caller-supplied DBTX.
func ApplyAuthority(ctx context.Context, db DBTX, in IncomingAuthority) (ApplyResult, error) {
	authorityID, err := upsertAuthority(ctx, db, in)
	if err != nil {
		return ApplyResult{}, err
	}

	res := ApplyResult{AuthorityID: authorityID}
	fields := []mutableField{
		{"website_url", in.WebsiteURL},
		{"archive_url", in.ArchiveURL},
		{"contact_email", in.ContactEmail},
		{"contact_phone", in.ContactPhone},
	}
	for _, f := range fields {
		applied, conflicts, err := resolveField(ctx, db, in, authorityID, f)
		if err != nil {
			return ApplyResult{}, err
		}
		res.Applied += applied
		res.Conflicts += conflicts
	}
	return res, nil
}

// upsertAuthority finds the authority by the unique (country_id,
// normalized_name, type) triple, inserting a stub row when absent. Only the
// identity and the always-required source_url/source_name columns are written
// here; the mutable fields are resolved per-field afterwards.
func upsertAuthority(ctx context.Context, db DBTX, in IncomingAuthority) (int64, error) {
	var id int64
	err := db.QueryRowContext(ctx, `
		SELECT id FROM authorities
		WHERE country_id = ? AND normalized_name = ? AND type = ?
	`, in.CountryID, in.NormalizedName, in.Type).Scan(&id)
	switch {
	case err == nil:
		return id, nil
	case err != sql.ErrNoRows:
		return 0, fmt.Errorf("lookup authority: %w", err)
	}

	res, err := db.ExecContext(ctx, `
		INSERT INTO authorities (
			country_id, normalized_name, name, type, source_url, source_name
		) VALUES (?, ?, ?, ?, ?, ?)
	`, in.CountryID, in.NormalizedName, in.Name, in.Type,
		fallback(in.SourceURL, in.SourceName), fallback(in.SourceName, in.SourceURL))
	if err != nil {
		return 0, fmt.Errorf("insert authority: %w", err)
	}
	id, err = res.LastInsertId()
	if err != nil {
		return 0, fmt.Errorf("authority last insert id: %w", err)
	}
	return id, nil
}

// resolveField applies the precedence rules to a single mutable field. It
// returns (appliedDelta, conflictDelta) for the call's running totals.
func resolveField(ctx context.Context, db DBTX, in IncomingAuthority, authorityID int64, f mutableField) (int, int, error) {
	overrideID, overrideValue, hasOverride, err := activeOverride(ctx, db, authorityID, f.column)
	if err != nil {
		return 0, 0, err
	}
	current, err := currentValue(ctx, db, authorityID, f.column)
	if err != nil {
		return 0, 0, err
	}

	switch {
	case hasOverride:
		// The curated value always wins. Write it and attribute the override.
		if err := writeField(ctx, db, authorityID, f.column, overrideValue); err != nil {
			return 0, 0, err
		}
		if err := putProvenance(ctx, db, authorityID, f.column, overrideValue,
			"curated_override", sql.NullInt64{}, sql.NullInt64{Int64: overrideID, Valid: true}); err != nil {
			return 0, 0, err
		}
		conflicts := 0
		if f.incoming != "" && f.incoming != overrideValue {
			created, err := recordConflict(ctx, db, in, authorityID, f.column,
				current, f.incoming, sql.NullString{String: overrideValue, Valid: true}, "override_conflict")
			if err != nil {
				return 0, 0, err
			}
			conflicts += created
		}
		return 1, conflicts, nil

	case f.incoming != "":
		// No override and a non-empty incoming value: apply the snapshot value.
		if err := writeField(ctx, db, authorityID, f.column, f.incoming); err != nil {
			return 0, 0, err
		}
		if err := putProvenance(ctx, db, authorityID, f.column, f.incoming,
			"icao_snapshot", sql.NullInt64{Int64: in.SnapshotID, Valid: true}, sql.NullInt64{}); err != nil {
			return 0, 0, err
		}
		return 1, 0, nil

	case current != "":
		// Incoming is empty while a value is on record: preserve it and flag the
		// upstream removal. The stored value and its provenance are untouched.
		created, err := recordConflict(ctx, db, in, authorityID, f.column,
			current, "", sql.NullString{}, "upstream_removal")
		if err != nil {
			return 0, 0, err
		}
		return 0, created, nil

	default:
		// Nothing on record and nothing incoming: nothing to do.
		return 0, 0, nil
	}
}

// activeOverride returns the active field_overrides row for an authority field.
func activeOverride(ctx context.Context, db DBTX, authorityID int64, field string) (id int64, value string, ok bool, err error) {
	var v sql.NullString
	err = db.QueryRowContext(ctx, `
		SELECT id, value FROM field_overrides
		WHERE entity_type = 'authority' AND entity_id = ? AND field_name = ? AND active = 1
	`, authorityID, field).Scan(&id, &v)
	switch {
	case err == sql.ErrNoRows:
		return 0, "", false, nil
	case err != nil:
		return 0, "", false, fmt.Errorf("lookup override %s: %w", field, err)
	}
	return id, v.String, true, nil
}

// currentValue reads the current stored value of an authority field ("" for NULL).
func currentValue(ctx context.Context, db DBTX, authorityID int64, field string) (string, error) {
	var v sql.NullString
	err := db.QueryRowContext(ctx,
		`SELECT `+field+` FROM authorities WHERE id = ?`, authorityID,
	).Scan(&v)
	if err != nil {
		return "", fmt.Errorf("read current %s: %w", field, err)
	}
	return v.String, nil
}

// writeField sets an authority field, storing NULL for an empty string.
func writeField(ctx context.Context, db DBTX, authorityID int64, field, value string) error {
	if _, err := db.ExecContext(ctx,
		`UPDATE authorities SET `+field+` = ? WHERE id = ?`, nullable(value), authorityID,
	); err != nil {
		return fmt.Errorf("write %s: %w", field, err)
	}
	return nil
}

// putProvenance upserts the single authority_field_provenance row for a field.
// Callers pass exactly the snapshot_id/override_id permitted for the kind so the
// table's self-consistency CHECK is always satisfied.
func putProvenance(ctx context.Context, db DBTX, authorityID int64, field, value, kind string, snapshotID, overrideID sql.NullInt64) error {
	if _, err := db.ExecContext(ctx, `
		INSERT INTO authority_field_provenance (
			authority_id, field_name, effective_value, provenance_kind,
			snapshot_id, override_id, updated_at
		) VALUES (?, ?, ?, ?, ?, ?, CAST(unixepoch('subsec') * 1000 AS INTEGER))
		ON CONFLICT(authority_id, field_name) DO UPDATE SET
			effective_value = excluded.effective_value,
			provenance_kind = excluded.provenance_kind,
			snapshot_id     = excluded.snapshot_id,
			override_id     = excluded.override_id,
			updated_at      = excluded.updated_at
	`, authorityID, field, nullable(value), kind, snapshotID, overrideID); err != nil {
		return fmt.Errorf("upsert provenance %s: %w", field, err)
	}
	return nil
}

// recordConflict inserts an open import_conflicts row idempotently. It returns 1
// when a new row was created and 0 when the idempotency index suppressed it.
func recordConflict(ctx context.Context, db DBTX, in IncomingAuthority, authorityID int64, field, current, incoming string, overrideValue sql.NullString, reason string) (int, error) {
	res, err := db.ExecContext(ctx, `
		INSERT INTO import_conflicts (
			import_run_id, target_entity_type, target_entity_id, field_name,
			current_value, incoming_value, override_value, reason, review_status
		) VALUES (?, 'authority', ?, ?, ?, ?, ?, ?, 'open')
		ON CONFLICT DO NOTHING
	`, in.RunID, authorityID, field,
		nullable(current), nullable(incoming), overrideValue, reason)
	if err != nil {
		return 0, fmt.Errorf("record conflict %s: %w", field, err)
	}
	affected, err := res.RowsAffected()
	if err != nil {
		return 0, fmt.Errorf("conflict rows affected: %w", err)
	}
	return int(affected), nil
}

// normalize lowercases and collapses whitespace for the normalized_name column.
// Exposed for test setup; the importer supplies its own normalized name at run
// time, so this is a deterministic helper rather than the canonical normalizer.
func normalize(name string) string {
	return strings.Join(strings.Fields(strings.ToLower(name)), " ")
}

// nullable maps an empty string to NULL so optional TEXT columns store NULL.
func nullable(s string) sql.NullString {
	return sql.NullString{String: s, Valid: s != ""}
}

// fallback returns a when non-empty, otherwise b. Used so the NOT NULL
// source_url/source_name columns always receive a value on stub insert.
func fallback(a, b string) string {
	if a != "" {
		return a
	}
	return b
}

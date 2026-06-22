package aia

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/effective"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/importer/common"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/model"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/provenance"
)

const (
	importerName = "aia"
	// aiaSourceType and aiaCanonicalURL identify the seeded ICAO AIA source row.
	aiaSourceType   = "regulator"
	aiaCanonicalURL = "https://www.icao.int/safety/airnavigation/AIG/Pages/AIA-States.aspx"
	authorityType   = "national_aai"
)

// Import parses the ICAO AIA directory carried by input and applies it to the
// canonical model. The flow is transactional: staging plus every ApplyAuthority
// call run inside one transaction, so a failure after the snapshot is recorded
// leaves no partial canonical change.
//
// Status semantics:
//   - unchanged: the body checksum was already seen; nothing is re-applied.
//   - success:   every parsed record was usable and applied (no unresolved/warn).
//   - partial:   usable records were applied but some records were unresolved,
//     malformed, or carried warnings.
//   - failed:    a fetch/snapshot/schema/transaction error; no canonical change.
func Import(ctx context.Context, db *sql.DB, input common.Input) (common.Result, error) {
	run, err := provenance.StartRun(ctx, db, importerName, input.SourceURL)
	if err != nil {
		return common.Result{}, fmt.Errorf("aia: start run: %w", err)
	}

	// tx is declared here so fail() can roll it back before calling FinishRun.
	// With SetMaxOpenConns(1), the single connection is held by the transaction,
	// so FinishRun (which uses the raw *sql.DB) would deadlock if tx were still
	// open when it runs.
	var tx *sql.Tx
	txDone := false

	// fail rolls back the open transaction (if any) FIRST, freeing the single
	// connection, then marks the run as failed.
	fail := func(err error) (common.Result, error) {
		if tx != nil && !txDone {
			_ = tx.Rollback()
			txDone = true
		}
		_ = provenance.FinishRun(ctx, db, run.ID, provenance.RunResult{
			Status: "failed", ErrorSummary: err.Error(),
		})
		return common.Result{RunID: run.ID, Status: "failed"}, err
	}

	sourceID, err := resolveSourceID(ctx, db, input.SourceURL)
	if err != nil {
		return fail(fmt.Errorf("aia: resolve source: %w", err))
	}

	snap, created, err := provenance.PutSnapshot(ctx, db, provenance.SnapshotInput{
		SourceID:     sourceID,
		SourceURL:    input.SourceURL,
		FinalURL:     input.FinalURL,
		StatusCode:   input.StatusCode,
		ContentType:  input.ContentType,
		ETag:         input.ETag,
		LastModified: input.LastModified,
		FetchedAt:    input.FetchedAt,
		Body:         input.Body,
	})
	if err != nil {
		return fail(fmt.Errorf("aia: put snapshot: %w", err))
	}
	if !created {
		// Identical body already imported: do not re-apply.
		if err := provenance.FinishRun(ctx, db, run.ID, provenance.RunResult{
			Status: "unchanged",
		}); err != nil {
			return fail(fmt.Errorf("aia: finish unchanged run: %w", err))
		}
		return common.Result{RunID: run.ID, Status: "unchanged", Unchanged: true}, nil
	}

	records, err := Parse(bytes.NewReader(input.Body))
	if err != nil {
		return fail(fmt.Errorf("aia: parse: %w", err))
	}

	countries, err := loadCountryAliases(ctx, db)
	if err != nil {
		return fail(fmt.Errorf("aia: load countries: %w", err))
	}

	tx, err = db.BeginTx(ctx, nil)
	if err != nil {
		return fail(fmt.Errorf("aia: begin tx: %w", err))
	}
	defer func() {
		if !txDone {
			_ = tx.Rollback()
		}
	}()

	res := common.Result{RunID: run.ID, Parsed: len(records)}

	for _, rec := range records {
		countryID, resolved := resolveCountry(rec, countries)

		if err := stageAuthority(ctx, tx, run.ID, rec, countryID, resolved); err != nil {
			return fail(fmt.Errorf("aia: stage %q: %w", rec.CountryLabel, err))
		}

		// Parser warnings always count, even on otherwise-applied records.
		res.Warnings += len(rec.Warnings)

		// Records that delegate to another State/body, that did not resolve to a
		// seeded country, or that lack a usable authority name are not applied as
		// canonical authorities. They are preserved in staging and surfaced as
		// warnings for review.
		delegated := rec.ReferenceCountry != "" || rec.ReferenceBody != ""
		if !resolved || delegated || rec.AuthorityName == "" {
			res.Warnings++
			continue
		}

		applied, err := effective.ApplyAuthority(ctx, tx, effective.IncomingAuthority{
			RunID:          run.ID,
			CountryID:      countryID,
			Name:           rec.AuthorityName,
			NormalizedName: model.NormalizeName(rec.AuthorityName),
			Type:           authorityType,
			WebsiteURL:     rec.WebsiteURL,
			ArchiveURL:     rec.ArchiveURL,
			ContactEmail:   firstOrEmpty(rec.Emails),
			ContactPhone:   firstOrEmpty(rec.Phones),
			SourceURL:      input.SourceURL,
			SourceName:     "ICAO AIA Member States Page",
			SnapshotID:     snap.ID,
		})
		if err != nil {
			return fail(fmt.Errorf("aia: apply %q: %w", rec.CountryLabel, err))
		}
		res.Applied++
		res.Conflicts += applied.Conflicts
	}

	if err := tx.Commit(); err != nil {
		return fail(fmt.Errorf("aia: commit: %w", err))
	}
	txDone = true

	// A run is partial when usable records were applied but some records were
	// unresolved, malformed, or conflicted; otherwise it is a clean success.
	res.Status = "success"
	if res.Warnings > 0 || res.Conflicts > 0 {
		res.Status = "partial"
	}

	if err := provenance.FinishRun(ctx, db, run.ID, provenance.RunResult{
		Status:    res.Status,
		Parsed:    res.Parsed,
		Applied:   res.Applied,
		Warnings:  res.Warnings,
		Conflicts: res.Conflicts,
	}); err != nil {
		return fail(fmt.Errorf("aia: finish run: %w", err))
	}

	return res, nil
}

// resolveSourceID finds the seeded ICAO AIA source row. It matches by the input
// URL first (canonical_url or url) and falls back to the known AIA canonical URL
// + source_type so an offline run with the canonical URL still resolves.
func resolveSourceID(ctx context.Context, db *sql.DB, sourceURL string) (int64, error) {
	var id int64
	err := db.QueryRowContext(ctx, `
		SELECT id FROM sources
		WHERE (canonical_url = ? OR url = ?) AND source_type = ?
		LIMIT 1
	`, sourceURL, sourceURL, aiaSourceType).Scan(&id)
	if err == nil {
		return id, nil
	}
	if err != sql.ErrNoRows {
		return 0, err
	}
	err = db.QueryRowContext(ctx, `
		SELECT id FROM sources
		WHERE canonical_url = ? AND source_type = ?
		LIMIT 1
	`, aiaCanonicalURL, aiaSourceType).Scan(&id)
	if err != nil {
		return 0, fmt.Errorf("seeded AIA source not found: %w", err)
	}
	return id, nil
}

// loadCountryAliases builds a normalized-name → country_id map from the seeded
// countries plus curated aliases for the short forms the directory uses (e.g.
// "United Kingdom" for the full ISO name).
func loadCountryAliases(ctx context.Context, db *sql.DB) (map[string]int64, error) {
	rows, err := db.QueryContext(ctx, `SELECT id, name FROM countries`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	aliases := map[string]int64{}
	for rows.Next() {
		var id int64
		var name string
		if err := rows.Scan(&id, &name); err != nil {
			return nil, err
		}
		aliases[model.NormalizeName(name)] = id
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}

	// Curated short-form aliases for the few States whose seeded ISO name differs
	// from the label the directory uses. Only added when the target country was
	// seeded, so the map never points at a non-existent id.
	for short, full := range curatedCountryAliases {
		if id, ok := aliases[model.NormalizeName(full)]; ok {
			aliases[model.NormalizeName(short)] = id
		}
	}
	return aliases, nil
}

// curatedCountryAliases maps a directory short form to the seeded ISO name.
var curatedCountryAliases = map[string]string{
	"united kingdom": "United Kingdom of Great Britain and Northern Ireland",
}

// resolveCountry resolves a record's country label (or its "Refer to" delegation
// target) to a seeded country id.
func resolveCountry(rec Record, aliases map[string]int64) (int64, bool) {
	if id, ok := aliases[model.NormalizeName(rec.CountryLabel)]; ok {
		return id, true
	}
	return 0, false
}

// stageAuthority inserts one parsed record into staged_authorities, including
// unresolved and malformed records, so nothing is dropped before review.
func stageAuthority(ctx context.Context, tx *sql.Tx, runID int64, rec Record, countryID int64, resolved bool) error {
	var warnings []string
	warnings = append(warnings, rec.Warnings...)
	if !resolved {
		warnings = append(warnings, "country label did not resolve to a seeded ISO country")
	}
	if rec.ReferenceCountry != "" {
		warnings = append(warnings, "delegated to State: "+rec.ReferenceCountry)
	}
	if rec.ReferenceBody != "" {
		warnings = append(warnings, "delegated to regional body: "+rec.ReferenceBody)
	}

	warningsJSON, err := marshalWarnings(warnings)
	if err != nil {
		return err
	}

	var resolvedCountry any
	if resolved {
		resolvedCountry = countryID
	}
	var updated any
	if rec.UpdatedAt != nil {
		updated = rec.UpdatedAt.Format("2006-01-02")
	}

	_, err = tx.ExecContext(ctx, `
		INSERT INTO staged_authorities (
			import_run_id, country_label, resolved_country_id, authority_name,
			raw_contact, website_url, archive_url, contact_email, contact_phone,
			icao_updated_date, warnings_json, record_checksum
		) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(import_run_id, record_checksum) DO NOTHING
	`,
		runID,
		rec.CountryLabel,
		resolvedCountry,
		fallback(rec.AuthorityName, "(unspecified)"),
		nullable(rec.RawContact),
		nullable(rec.WebsiteURL),
		nullable(rec.ArchiveURL),
		nullable(firstOrEmpty(rec.Emails)),
		nullable(firstOrEmpty(rec.Phones)),
		updated,
		warningsJSON,
		rec.Checksum,
	)
	if err != nil {
		return fmt.Errorf("insert staged authority: %w", err)
	}
	return nil
}

// marshalWarnings returns a JSON array of warnings, or NULL when there are none.
func marshalWarnings(warnings []string) (any, error) {
	if len(warnings) == 0 {
		return nil, nil
	}
	b, err := json.Marshal(warnings)
	if err != nil {
		return nil, fmt.Errorf("marshal warnings: %w", err)
	}
	return string(b), nil
}

func firstOrEmpty(ss []string) string {
	if len(ss) == 0 {
		return ""
	}
	return ss[0]
}

func nullable(s string) sql.NullString {
	return sql.NullString{String: s, Valid: s != ""}
}

func fallback(a, b string) string {
	if strings.TrimSpace(a) != "" {
		return a
	}
	return b
}

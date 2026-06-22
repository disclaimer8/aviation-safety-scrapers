package raio

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"fmt"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/importer/common"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/model"
	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/provenance"
)

const (
	importerName = "raio"
	// raioSourceType and raioCanonicalURL identify the seeded ICAO RAIO source row.
	raioSourceType   = "regulator"
	raioCanonicalURL = "https://www.icao.int/safety/airnavigation/AIG/Pages/Regional-Accident-Incident-Investigation-Organizations.aspx"
)

// coverageUpgradeable is the set of coverage states a RAIO member may be raised
// out of when ICAO confirms RAIO membership. Any other state (e.g. a curated
// delegation or an existing direct archive) is authoritative and never
// downgraded. ICM membership never triggers this upgrade at all.
var coverageUpgradeable = map[string]bool{
	"unknown":               true,
	"official_contact_only": true,
	"no_public_archive":     true,
}

// Import parses the ICAO RAIO/ICM list carried by input and applies it to the
// canonical model. The flow is transactional: staging plus every membership and
// coverage write run inside one transaction, so a failure after the snapshot is
// recorded leaves no partial canonical change.
//
// Safety rules:
//   - Source-derived memberships are upserted; curated memberships absent from
//     ICAO are never deleted (a RAIO import that omits ECCAA leaves ECCAA intact).
//   - coverage_status='regional_raio' is set only for RAIO members whose current
//     coverage is upgradeable; ICM membership never changes coverage.
//   - Unresolved member labels become warnings and a partial run, not a rollback.
//
// Status semantics:
//   - unchanged: the body checksum was already seen; nothing is re-applied.
//   - success:   every parsed body and member resolved and applied cleanly.
//   - partial:   applied, but some labels were unresolved or bodies carried
//     warnings.
//   - failed:    a fetch/snapshot/schema/transaction error; no canonical change.
func Import(ctx context.Context, db *sql.DB, input common.Input) (common.Result, error) {
	run, err := provenance.StartRun(ctx, db, importerName, input.SourceURL)
	if err != nil {
		return common.Result{}, fmt.Errorf("raio: start run: %w", err)
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
		return fail(fmt.Errorf("raio: resolve source: %w", err))
	}

	_, created, err := provenance.PutSnapshot(ctx, db, provenance.SnapshotInput{
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
		return fail(fmt.Errorf("raio: put snapshot: %w", err))
	}
	if !created {
		if err := provenance.FinishRun(ctx, db, run.ID, provenance.RunResult{
			Status: "unchanged",
		}); err != nil {
			return fail(fmt.Errorf("raio: finish unchanged run: %w", err))
		}
		return common.Result{RunID: run.ID, Status: "unchanged", Unchanged: true}, nil
	}

	records, err := Parse(bytes.NewReader(input.Body))
	if err != nil {
		return fail(fmt.Errorf("raio: parse: %w", err))
	}

	countries, err := loadCountryNames(ctx, db)
	if err != nil {
		return fail(fmt.Errorf("raio: load countries: %w", err))
	}

	tx, err = db.BeginTx(ctx, nil)
	if err != nil {
		return fail(fmt.Errorf("raio: begin tx: %w", err))
	}
	defer func() {
		if !txDone {
			_ = tx.Rollback()
		}
	}()

	res := common.Result{RunID: run.ID, Parsed: len(records)}

	for _, rec := range records {
		warnings := append([]string(nil), rec.Warnings...)

		// Resolve every member and observer label up front; unresolved labels are
		// preserved as warnings, never dropped.
		memberIDs, memberWarn := resolveLabels(rec.Members, countries, "member")
		observerIDs, observerWarn := resolveLabels(rec.Observers, countries, "observer")
		warnings = append(warnings, memberWarn...)
		warnings = append(warnings, observerWarn...)

		if err := stageBody(ctx, tx, run.ID, rec, warnings); err != nil {
			return fail(fmt.Errorf("raio: stage %q: %w", rec.Code, err))
		}
		res.Warnings += len(warnings)

		bodyID, err := upsertBody(ctx, tx, rec)
		if err != nil {
			return fail(fmt.Errorf("raio: upsert body %q: %w", rec.Code, err))
		}

		// Source-derived memberships (role 'member'). Curated memberships absent
		// from ICAO are deliberately left in place — no DELETE.
		for _, cid := range memberIDs {
			if err := upsertMember(ctx, tx, bodyID, cid, "member", input.SourceURL); err != nil {
				return fail(fmt.Errorf("raio: upsert member: %w", err))
			}
			res.Applied++
			// Conditional coverage upgrade: RAIO members only.
			if rec.Class == "raio" {
				if err := upgradeCoverage(ctx, tx, cid, input.SourceURL); err != nil {
					return fail(fmt.Errorf("raio: coverage upgrade: %w", err))
				}
			}
		}
		// Observers (role 'observer'); never affect coverage.
		for _, cid := range observerIDs {
			if err := upsertMember(ctx, tx, bodyID, cid, "observer", input.SourceURL); err != nil {
				return fail(fmt.Errorf("raio: upsert observer: %w", err))
			}
			res.Applied++
		}
	}

	if err := tx.Commit(); err != nil {
		return fail(fmt.Errorf("raio: commit: %w", err))
	}
	txDone = true

	res.Status = "success"
	if res.Warnings > 0 {
		res.Status = "partial"
	}

	if err := provenance.FinishRun(ctx, db, run.ID, provenance.RunResult{
		Status:   res.Status,
		Parsed:   res.Parsed,
		Applied:  res.Applied,
		Warnings: res.Warnings,
	}); err != nil {
		return fail(fmt.Errorf("raio: finish run: %w", err))
	}

	return res, nil
}

// resolveSourceID finds the seeded ICAO RAIO source row. It matches by the input
// URL first and falls back to the known RAIO canonical URL + source_type so an
// offline run with the canonical URL still resolves.
func resolveSourceID(ctx context.Context, db *sql.DB, sourceURL string) (int64, error) {
	var id int64
	err := db.QueryRowContext(ctx, `
		SELECT id FROM sources
		WHERE (canonical_url = ? OR url = ?) AND source_type = ?
		LIMIT 1
	`, sourceURL, sourceURL, raioSourceType).Scan(&id)
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
	`, raioCanonicalURL, raioSourceType).Scan(&id)
	if err != nil {
		return 0, fmt.Errorf("seeded RAIO source not found: %w", err)
	}
	return id, nil
}

// loadCountryNames builds a normalized-name → country_id map from the seeded
// countries, plus curated aliases for the short forms the ICAO page uses.
func loadCountryNames(ctx context.Context, db *sql.DB) (map[string]int64, error) {
	rows, err := db.QueryContext(ctx, `SELECT id, name FROM countries`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	names := map[string]int64{}
	for rows.Next() {
		var id int64
		var name string
		if err := rows.Scan(&id, &name); err != nil {
			return nil, err
		}
		names[model.NormalizeName(name)] = id
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}

	for short, full := range curatedCountryAliases {
		if id, ok := names[model.NormalizeName(full)]; ok {
			names[model.NormalizeName(short)] = id
		}
	}
	return names, nil
}

// curatedCountryAliases maps an ICAO page short form to the seeded ISO name.
var curatedCountryAliases = map[string]string{
	"russia":  "Russian Federation",
	"moldova": "Republic of Moldova",
}

// resolveLabels maps a list of raw State labels to seeded country ids. Each
// unresolved label is returned as a warning so it is preserved for review.
func resolveLabels(labels []string, names map[string]int64, role string) (ids []int64, warnings []string) {
	for _, label := range labels {
		if id, ok := names[model.NormalizeName(label)]; ok {
			ids = append(ids, id)
			continue
		}
		warnings = append(warnings, fmt.Sprintf("unresolved %s label: %q", role, label))
	}
	return ids, warnings
}

// stageBody inserts one parsed body into staged_regional_bodies, including its
// member/observer labels and warnings, so nothing is dropped before review.
func stageBody(ctx context.Context, tx *sql.Tx, runID int64, rec BodyRecord, warnings []string) error {
	memberJSON, err := marshalJSON(rec.Members)
	if err != nil {
		return err
	}
	observerJSON, err := marshalJSON(rec.Observers)
	if err != nil {
		return err
	}
	warnJSON, err := marshalJSON(warnings)
	if err != nil {
		return err
	}

	_, err = tx.ExecContext(ctx, `
		INSERT INTO staged_regional_bodies (
			import_run_id, code, description, region, website_url, body_class,
			member_labels_json, observer_labels_json, warnings_json, record_checksum
		) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(import_run_id, record_checksum) DO NOTHING
	`,
		runID,
		rec.Code,
		nullable(rec.Name),
		nullable(rec.Region),
		nullable(rec.WebsiteURL),
		rec.Class,
		memberJSON,
		observerJSON,
		warnJSON,
		rec.Checksum,
	)
	if err != nil {
		return fmt.Errorf("insert staged regional body: %w", err)
	}
	return nil
}

// upsertBody upserts the body identity + website keyed by code, and returns the
// body id. Curated name/notes are preserved; the website is refreshed from ICAO
// when present. body_class is left untouched on update so a curated class
// (e.g. 'regional_body') is not overwritten by the parser's coarse class.
func upsertBody(ctx context.Context, tx *sql.Tx, rec BodyRecord) (int64, error) {
	name := rec.Name
	if name == "" {
		name = rec.Code
	}
	_, err := tx.ExecContext(ctx, `
		INSERT INTO regional_bodies (code, name, body_class, website_url, source_url)
		VALUES (?, ?, ?, ?, ?)
		ON CONFLICT(code) DO UPDATE SET
			website_url = COALESCE(NULLIF(excluded.website_url, ''), regional_bodies.website_url)
	`, rec.Code, name, rec.Class, nullable(rec.WebsiteURL), raioCanonicalURL)
	if err != nil {
		return 0, err
	}
	var id int64
	if err := tx.QueryRowContext(ctx, `SELECT id FROM regional_bodies WHERE code = ?`, rec.Code).Scan(&id); err != nil {
		return 0, err
	}
	return id, nil
}

// upsertMember inserts a (body, country, role) membership, idempotent on the
// composite primary key. It never deletes existing rows.
func upsertMember(ctx context.Context, tx *sql.Tx, bodyID, countryID int64, role, sourceURL string) error {
	_, err := tx.ExecContext(ctx, `
		INSERT INTO regional_body_members (regional_body_id, country_id, role, source_url)
		VALUES (?, ?, ?, ?)
		ON CONFLICT(regional_body_id, country_id, role) DO UPDATE SET
			source_url = excluded.source_url
	`, bodyID, countryID, role, sourceURL)
	return err
}

// upgradeCoverage raises a RAIO member's coverage_status to 'regional_raio' only
// when its current status is upgradeable. The WHERE clause enforces the rule
// atomically so a non-upgradeable state (e.g. 'direct_public_archive') is never
// downgraded.
func upgradeCoverage(ctx context.Context, tx *sql.Tx, countryID int64, sourceURL string) error {
	upgradeable := make([]any, 0, len(coverageUpgradeable))
	placeholders := ""
	for status := range coverageUpgradeable {
		if placeholders != "" {
			placeholders += ","
		}
		placeholders += "?"
		upgradeable = append(upgradeable, status)
	}
	args := append([]any{countryID}, upgradeable...)
	_, err := tx.ExecContext(ctx, `
		UPDATE countries SET coverage_status = 'regional_raio'
		WHERE id = ? AND coverage_status IN (`+placeholders+`)
	`, args...)
	return err
}

// marshalJSON returns a JSON array, or NULL when the slice is empty.
func marshalJSON(ss []string) (any, error) {
	if len(ss) == 0 {
		return nil, nil
	}
	b, err := json.Marshal(ss)
	if err != nil {
		return nil, fmt.Errorf("marshal json: %w", err)
	}
	return string(b), nil
}

func nullable(s string) sql.NullString {
	return sql.NullString{String: s, Valid: s != ""}
}

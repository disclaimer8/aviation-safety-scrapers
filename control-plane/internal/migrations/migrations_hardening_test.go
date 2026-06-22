package migrations

import (
	"database/sql"
	"sort"
	"testing"
)

// insertSnapshot inserts an immutable source snapshot and returns its id.
func insertSnapshot(t *testing.T, db *sql.DB, sourceID int64, checksum string) int64 {
	t.Helper()

	result, err := db.Exec(`
		INSERT INTO source_snapshots (
			source_id, source_url, fetched_at, checksum, raw_body, size_bytes
		) VALUES (?, 'https://example.test/doc', 1000, ?, X'00', 1)
	`, sourceID, checksum)
	if err != nil {
		t.Fatal(err)
	}
	id, err := result.LastInsertId()
	if err != nil {
		t.Fatal(err)
	}
	return id
}

func insertAuthority(t *testing.T, db *sql.DB, countryID int64, normalized string) int64 {
	t.Helper()

	result, err := db.Exec(`
		INSERT INTO authorities (
			country_id, normalized_name, name, type, source_url, source_name
		) VALUES (?, ?, ?, 'national_aai', 'https://example.test', 'test')
	`, countryID, normalized, normalized)
	if err != nil {
		t.Fatal(err)
	}
	id, err := result.LastInsertId()
	if err != nil {
		t.Fatal(err)
	}
	return id
}

// -----------------------------------------------------------------------------
// 17.5.B — field-level authority provenance
// -----------------------------------------------------------------------------

// Two fields on one authority can reference different snapshots.
func TestProvenanceTwoFieldsDifferentSnapshots(t *testing.T) {
	db := applyTestSchema(t)

	countryID := insertCountry(t, db)
	sourceID := insertSource(t, db)
	authorityID := insertAuthority(t, db, countryID, "alpha")
	snapA := insertSnapshot(t, db, sourceID, "checksum-a")
	snapB := insertSnapshot(t, db, sourceID, "checksum-b")

	insertProv := `
		INSERT INTO authority_field_provenance (
			authority_id, field_name, effective_value, provenance_kind, snapshot_id
		) VALUES (?, ?, ?, 'icao_snapshot', ?)
	`
	if _, err := db.Exec(insertProv, authorityID, "contact_email", "a@x.test", snapA); err != nil {
		t.Fatalf("insert email provenance: %v", err)
	}
	if _, err := db.Exec(insertProv, authorityID, "contact_phone", "+1", snapB); err != nil {
		t.Fatalf("insert phone provenance: %v", err)
	}

	var emailSnap, phoneSnap int64
	if err := db.QueryRow(
		`SELECT snapshot_id FROM authority_field_provenance WHERE authority_id=? AND field_name='contact_email'`,
		authorityID,
	).Scan(&emailSnap); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow(
		`SELECT snapshot_id FROM authority_field_provenance WHERE authority_id=? AND field_name='contact_phone'`,
		authorityID,
	).Scan(&phoneSnap); err != nil {
		t.Fatal(err)
	}
	if emailSnap != snapA || phoneSnap != snapB {
		t.Fatalf("email snapshot=%d (want %d), phone snapshot=%d (want %d)",
			emailSnap, snapA, phoneSnap, snapB)
	}
}

// An override affects only its field; other fields keep their own provenance.
func TestProvenanceOverrideAffectsOnlyItsField(t *testing.T) {
	db := applyTestSchema(t)

	countryID := insertCountry(t, db)
	sourceID := insertSource(t, db)
	authorityID := insertAuthority(t, db, countryID, "alpha")
	snap := insertSnapshot(t, db, sourceID, "checksum-a")

	override, err := db.Exec(`
		INSERT INTO field_overrides (
			entity_type, entity_id, field_name, value, value_type, reason, author
		) VALUES ('authority', ?, 'name', 'Official Name', 'text', 'manual', 'denys')
	`, authorityID)
	if err != nil {
		t.Fatal(err)
	}
	overrideID, err := override.LastInsertId()
	if err != nil {
		t.Fatal(err)
	}

	if _, err := db.Exec(`
		INSERT INTO authority_field_provenance (
			authority_id, field_name, effective_value, provenance_kind, snapshot_id
		) VALUES (?, 'contact_email', 'a@x.test', 'icao_snapshot', ?)
	`, authorityID, snap); err != nil {
		t.Fatalf("insert email provenance: %v", err)
	}
	if _, err := db.Exec(`
		INSERT INTO authority_field_provenance (
			authority_id, field_name, effective_value, provenance_kind, override_id
		) VALUES (?, 'name', 'Official Name', 'curated_override', ?)
	`, authorityID, overrideID); err != nil {
		t.Fatalf("insert name override provenance: %v", err)
	}

	var emailKind string
	if err := db.QueryRow(
		`SELECT provenance_kind FROM authority_field_provenance WHERE authority_id=? AND field_name='contact_email'`,
		authorityID,
	).Scan(&emailKind); err != nil {
		t.Fatal(err)
	}
	if emailKind != "icao_snapshot" {
		t.Fatalf("email provenance kind=%q, want icao_snapshot (override leaked across fields)", emailKind)
	}
}

// Updating one imported field's provenance does not change another field's.
func TestProvenanceUpdatingOneFieldLeavesOthers(t *testing.T) {
	db := applyTestSchema(t)

	countryID := insertCountry(t, db)
	sourceID := insertSource(t, db)
	authorityID := insertAuthority(t, db, countryID, "alpha")
	snapA := insertSnapshot(t, db, sourceID, "checksum-a")
	snapB := insertSnapshot(t, db, sourceID, "checksum-b")
	snapC := insertSnapshot(t, db, sourceID, "checksum-c")

	insertProv := `
		INSERT INTO authority_field_provenance (
			authority_id, field_name, effective_value, provenance_kind, snapshot_id
		) VALUES (?, ?, ?, 'icao_snapshot', ?)
	`
	if _, err := db.Exec(insertProv, authorityID, "contact_email", "a@x.test", snapA); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(insertProv, authorityID, "contact_phone", "+1", snapB); err != nil {
		t.Fatal(err)
	}

	// Re-source the email from a newer snapshot.
	if _, err := db.Exec(`
		UPDATE authority_field_provenance
		SET effective_value='b@x.test', snapshot_id=?
		WHERE authority_id=? AND field_name='contact_email'
	`, snapC, authorityID); err != nil {
		t.Fatal(err)
	}

	var phoneSnap int64
	if err := db.QueryRow(
		`SELECT snapshot_id FROM authority_field_provenance WHERE authority_id=? AND field_name='contact_phone'`,
		authorityID,
	).Scan(&phoneSnap); err != nil {
		t.Fatal(err)
	}
	if phoneSnap != snapB {
		t.Fatalf("phone snapshot=%d, want unchanged %d", phoneSnap, snapB)
	}
}

// Exactly one current provenance record per authority field is enforced.
func TestProvenanceEnforcesOneRecordPerField(t *testing.T) {
	db := applyTestSchema(t)

	countryID := insertCountry(t, db)
	sourceID := insertSource(t, db)
	authorityID := insertAuthority(t, db, countryID, "alpha")
	snap := insertSnapshot(t, db, sourceID, "checksum-a")

	insertProv := `
		INSERT INTO authority_field_provenance (
			authority_id, field_name, effective_value, provenance_kind, snapshot_id
		) VALUES (?, 'contact_email', ?, 'icao_snapshot', ?)
	`
	if _, err := db.Exec(insertProv, authorityID, "a@x.test", snap); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(insertProv, authorityID, "b@x.test", snap); err == nil {
		t.Fatal("expected duplicate (authority_id, field_name) provenance to be rejected")
	}
}

// Provenance kind and reference columns must be self-consistent.
func TestProvenanceKindReferenceConsistency(t *testing.T) {
	db := applyTestSchema(t)

	countryID := insertCountry(t, db)
	sourceID := insertSource(t, db)
	authorityID := insertAuthority(t, db, countryID, "alpha")
	snap := insertSnapshot(t, db, sourceID, "checksum-a")

	// snapshot kind without a snapshot id must fail.
	if _, err := db.Exec(`
		INSERT INTO authority_field_provenance (
			authority_id, field_name, provenance_kind
		) VALUES (?, 'contact_email', 'icao_snapshot')
	`, authorityID); err == nil {
		t.Fatal("expected icao_snapshot without snapshot_id to be rejected")
	}

	// seed kind with a snapshot id must fail.
	if _, err := db.Exec(`
		INSERT INTO authority_field_provenance (
			authority_id, field_name, provenance_kind, snapshot_id
		) VALUES (?, 'contact_email', 'seed', ?)
	`, authorityID, snap); err == nil {
		t.Fatal("expected seed with snapshot_id to be rejected")
	}
}

// -----------------------------------------------------------------------------
// 17.5.C — source snapshots are immutable
// -----------------------------------------------------------------------------

func TestSnapshotImmutability(t *testing.T) {
	db := applyTestSchema(t)

	countryID := insertCountry(t, db)
	sourceID := insertSource(t, db)
	authorityID := insertAuthority(t, db, countryID, "alpha")
	snap := insertSnapshot(t, db, sourceID, "checksum-a")

	otherSource, err := db.Exec(`
		INSERT INTO sources (name, url, canonical_url, source_type, source_tier)
		VALUES ('Other', 'https://other.test', 'https://other.test', 'official_aai', 2)
	`)
	if err != nil {
		t.Fatal(err)
	}
	otherSourceID, err := otherSource.LastInsertId()
	if err != nil {
		t.Fatal(err)
	}

	cases := []struct {
		name string
		stmt string
		args []any
	}{
		{"raw body", `UPDATE source_snapshots SET raw_body=X'01' WHERE id=?`, []any{snap}},
		{"checksum", `UPDATE source_snapshots SET checksum='other' WHERE id=?`, []any{snap}},
		{"source id", `UPDATE source_snapshots SET source_id=? WHERE id=?`, []any{otherSourceID, snap}},
		{"primary key", `UPDATE source_snapshots SET id=? WHERE id=?`, []any{snap + 100, snap}},
		{"final url", `UPDATE source_snapshots SET final_url='https://x.test/2' WHERE id=?`, []any{snap}},
		{"status code", `UPDATE source_snapshots SET status_code=500 WHERE id=?`, []any{snap}},
		{"fetched at", `UPDATE source_snapshots SET fetched_at=2000 WHERE id=?`, []any{snap}},
		{"size bytes", `UPDATE source_snapshots SET size_bytes=99 WHERE id=?`, []any{snap}},
		{"artifact path", `UPDATE source_snapshots SET artifact_path='/p' WHERE id=?`, []any{snap}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := db.Exec(tc.stmt, tc.args...); err == nil {
				t.Fatalf("expected immutable %s update to be rejected", tc.name)
			}
		})
	}

	// Source id matches and the source argument is the same value: this is a
	// no-op update and must succeed (idempotent re-write of identical bytes).
	t.Run("noop self-update allowed", func(t *testing.T) {
		if _, err := db.Exec(`UPDATE source_snapshots SET source_id=source_id WHERE id=?`, snap); err != nil {
			t.Fatalf("no-op self-update should be allowed: %v", err)
		}
	})

	// Deleting a snapshot referenced by an authority must fail (ON DELETE RESTRICT).
	t.Run("delete referenced snapshot fails", func(t *testing.T) {
		if _, err := db.Exec(`UPDATE authorities SET source_snapshot_id=? WHERE id=?`, snap, authorityID); err != nil {
			t.Fatal(err)
		}
		if _, err := db.Exec(`DELETE FROM source_snapshots WHERE id=?`, snap); err == nil {
			t.Fatal("expected deleting a referenced snapshot to be rejected")
		}
	})
}

// -----------------------------------------------------------------------------
// 17.5.D — strict SQLite typing
// -----------------------------------------------------------------------------

func TestStrictTypingRejectsFractionalAndTextualIntegers(t *testing.T) {
	db := applyTestSchema(t)
	countryID := insertCountry(t, db)
	sourceID := insertSource(t, db)

	t.Run("coverage_score fractional", func(t *testing.T) {
		_, err := db.Exec(`
			INSERT INTO countries (
				iso2, iso3, name, region, policy_status, coverage_status,
				coverage_score, effort_score, expected_records,
				expected_source_quality, priority_score
			) VALUES ('Q1','QQ1','Q','R','allowed','unknown', 2.5, 1, 0, 1, 0)
		`)
		if err == nil {
			t.Fatal("expected coverage_score=2.5 to be rejected by STRICT")
		}
	})

	t.Run("effort_score fractional", func(t *testing.T) {
		_, err := db.Exec(`
			INSERT INTO countries (
				iso2, iso3, name, region, policy_status, coverage_status,
				coverage_score, effort_score, expected_records,
				expected_source_quality, priority_score
			) VALUES ('Q2','QQ2','Q','R','allowed','unknown', 0, 2.5, 0, 1, 0)
		`)
		if err == nil {
			t.Fatal("expected fractional effort_score to be rejected")
		}
	})

	// STRICT losslessly coerces a text integer literal into the INTEGER storage
	// class rather than rejecting it; the invariant that matters is that the
	// stored value is an integer, never text. Verify the storage class.
	t.Run("expected_records textual integer stored as integer", func(t *testing.T) {
		if _, err := db.Exec(`
			INSERT INTO countries (
				iso2, iso3, name, region, policy_status, coverage_status,
				coverage_score, effort_score, expected_records,
				expected_source_quality, priority_score
			) VALUES ('Q3','QQ3','Q','R','allowed','unknown', 0, 1, '5', 1, 0)
		`); err != nil {
			t.Fatal(err)
		}
		var kind string
		if err := db.QueryRow(
			`SELECT typeof(expected_records) FROM countries WHERE iso2='Q3'`,
		).Scan(&kind); err != nil {
			t.Fatal(err)
		}
		if kind != "integer" {
			t.Fatalf("expected_records storage class=%q, want integer", kind)
		}
	})

	t.Run("source_tier fractional", func(t *testing.T) {
		_, err := db.Exec(`
			INSERT INTO sources (name, url, canonical_url, source_type, source_tier)
			VALUES ('s','https://a.test','https://a.test/2','official_aai', 1.5)
		`)
		if err == nil {
			t.Fatal("expected fractional source_tier to be rejected")
		}
	})

	t.Run("confidence_score fractional", func(t *testing.T) {
		_, err := db.Exec(`
			INSERT INTO events (
				date_precision, occurrence_country_id, event_type,
				investigation_status, confidence_score
			) VALUES ('unknown', ?, 'unknown', 'unknown', 50.5)
		`, countryID)
		if err == nil {
			t.Fatal("expected fractional confidence_score to be rejected")
		}
	})

	t.Run("textual integer foreign key stored as integer", func(t *testing.T) {
		res, err := db.Exec(`
			INSERT INTO events (
				date_precision, occurrence_country_id, event_type,
				investigation_status, confidence_score
			) VALUES ('unknown', ?, 'unknown', 'unknown', 50)
		`, "1")
		if err != nil {
			t.Fatal(err)
		}
		id, err := res.LastInsertId()
		if err != nil {
			t.Fatal(err)
		}
		var kind string
		if err := db.QueryRow(
			`SELECT typeof(occurrence_country_id) FROM events WHERE id=?`, id,
		).Scan(&kind); err != nil {
			t.Fatal(err)
		}
		if kind != "integer" {
			t.Fatalf("occurrence_country_id storage class=%q, want integer", kind)
		}
	})

	t.Run("nullable integer accepts null and integer", func(t *testing.T) {
		// nullable status_code on source_snapshots: null is fine, integer is fine.
		if _, err := db.Exec(`
			INSERT INTO source_snapshots (source_id, source_url, fetched_at, checksum, size_bytes, status_code)
			VALUES (?, 'https://a.test', 1, 'ck-null', 0, NULL)
		`, sourceID); err != nil {
			t.Fatalf("null status_code should be allowed: %v", err)
		}
		if _, err := db.Exec(`
			INSERT INTO source_snapshots (source_id, source_url, fetched_at, checksum, size_bytes, status_code)
			VALUES (?, 'https://a.test', 1, 'ck-int', 0, 200)
		`, sourceID); err != nil {
			t.Fatalf("integer status_code should be allowed: %v", err)
		}
		// A fractional REAL into the nullable integer column must be rejected.
		if _, err := db.Exec(`
			INSERT INTO source_snapshots (source_id, source_url, fetched_at, checksum, size_bytes, status_code)
			VALUES (?, 'https://a.test', 1, 'ck-frac', 0, 200.5)
		`, sourceID); err == nil {
			t.Fatal("expected fractional status_code to be rejected")
		}
	})
}

func TestAllDataTablesAreStrict(t *testing.T) {
	db := applyTestSchema(t)

	rows, err := db.Query(`
		SELECT name FROM sqlite_master
		WHERE type='table' AND name NOT LIKE 'sqlite_%'
		ORDER BY name
	`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()

	var tables []string
	for rows.Next() {
		var name string
		if err := rows.Scan(&name); err != nil {
			t.Fatal(err)
		}
		tables = append(tables, name)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}

	for _, table := range tables {
		var ddl string
		if err := db.QueryRow(
			`SELECT sql FROM sqlite_master WHERE type='table' AND name=?`, table,
		).Scan(&ddl); err != nil {
			t.Fatal(err)
		}
		if !hasStrict(ddl) {
			t.Errorf("table %q is not declared STRICT", table)
		}
	}
}

func hasStrict(ddl string) bool {
	// STRICT appears as a trailing table option after the closing paren.
	for i := len(ddl) - 1; i >= 0; i-- {
		if ddl[i] == ')' {
			tail := ddl[i+1:]
			return containsWord(tail, "STRICT")
		}
	}
	return false
}

func containsWord(s, word string) bool {
	for i := 0; i+len(word) <= len(s); i++ {
		if s[i:i+len(word)] == word {
			return true
		}
	}
	return false
}

// -----------------------------------------------------------------------------
// 17.5.E — expanded schema verification
// -----------------------------------------------------------------------------

func TestForeignKeyCheckIsClean(t *testing.T) {
	db := applyTestSchema(t)

	// Exercise the FK graph with a realistic chain, then assert no violations.
	countryID := insertCountry(t, db)
	sourceID := insertSource(t, db)
	authorityID := insertAuthority(t, db, countryID, "alpha")
	snap := insertSnapshot(t, db, sourceID, "checksum-a")
	if _, err := db.Exec(`UPDATE authorities SET source_snapshot_id=? WHERE id=?`, snap, authorityID); err != nil {
		t.Fatal(err)
	}

	rows, err := db.Query(`PRAGMA foreign_key_check`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	if rows.Next() {
		t.Fatal("PRAGMA foreign_key_check reported violations")
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
}

func TestExactRequiredIndexSet(t *testing.T) {
	db := applyTestSchema(t)

	rows, err := db.Query(`
		SELECT name FROM sqlite_master
		WHERE type='index' AND name NOT LIKE 'sqlite_%'
		ORDER BY name
	`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()

	var got []string
	for rows.Next() {
		var name string
		if err := rows.Scan(&name); err != nil {
			t.Fatal(err)
		}
		got = append(got, name)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}

	want := []string{
		"idx_active_field_override",
		"idx_aircraft_routes_authority",
		"idx_aircraft_routes_design_country",
		"idx_aircraft_routes_manufacture_country",
		"idx_authorities_country",
		"idx_authority_field_provenance_override",
		"idx_authority_field_provenance_snapshot",
		"idx_authority_requests_authority",
		"idx_crawl_errors_job",
		"idx_crawl_jobs_country",
		"idx_crawl_jobs_source",
		"idx_crawl_jobs_status_type",
		"idx_event_source_links_event",
		"idx_event_source_links_matched",
		"idx_event_source_links_source",
		"idx_event_source_native_id",
		"idx_events_date_registration_country",
		"idx_events_fallback_match",
		"idx_events_occurrence_country",
		"idx_import_conflicts_idempotent",
		"idx_import_conflicts_open",
		"idx_import_conflicts_run",
		"idx_import_conflicts_staged_authority",
		"idx_import_conflicts_staged_regional_body",
		"idx_import_runs_snapshot",
		"idx_investigation_participants_authority",
		"idx_investigation_participants_country",
		"idx_investigation_participants_event",
		"idx_regional_body_members_country",
		"idx_reports_event",
		"idx_reports_source",
		"idx_snapshots_source_checksum",
		"idx_source_snapshots_source",
		"idx_staged_authorities_country",
		"idx_staged_authorities_run",
		"idx_staged_regional_bodies_run",
	}
	sort.Strings(want)

	if len(got) != len(want) {
		t.Fatalf("index count=%d, want %d\n got=%v\nwant=%v", len(got), len(want), got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("index[%d]=%q, want %q\n got=%v\nwant=%v", i, got[i], want[i], got, want)
		}
	}
}

func TestExactRequiredDefaults(t *testing.T) {
	db := applyTestSchema(t)

	cases := []struct {
		table  string
		column string
		want   string
	}{
		{"authorities", "status", "'unknown'"},
		{"sources", "health_status", "'unknown'"},
		{"sources", "active", "1"},
		{"events", "event_type", "'unknown'"},
		{"events", "investigation_status", "'unknown'"},
		{"events", "date_precision", "'unknown'"},
		{"events", "dedup_status", "'unreviewed'"},
		{"events", "needs_official_confirmation", "0"},
		{"reports", "extraction_status", "'pending'"},
		{"crawl_jobs", "status", "'pending'"},
		{"import_conflicts", "review_status", "'open'"},
		{"authority_requests", "status", "'not_sent'"},
		{"field_overrides", "active", "1"},
		{"countries", "expected_records", "0"},
		{"countries", "expected_source_quality", "1"},
	}

	for _, c := range cases {
		t.Run(c.table+"."+c.column, func(t *testing.T) {
			var dflt sql.NullString
			rows, err := db.Query(`SELECT name, dflt_value FROM pragma_table_info(?)`, c.table)
			if err != nil {
				t.Fatal(err)
			}
			defer rows.Close()
			found := false
			for rows.Next() {
				var name string
				var d sql.NullString
				if err := rows.Scan(&name, &d); err != nil {
					t.Fatal(err)
				}
				if name == c.column {
					dflt = d
					found = true
				}
			}
			if err := rows.Err(); err != nil {
				t.Fatal(err)
			}
			if !found {
				t.Fatalf("column %s.%s not found", c.table, c.column)
			}
			if !dflt.Valid || dflt.String != c.want {
				t.Fatalf("%s.%s default=%q (valid=%v), want %q",
					c.table, c.column, dflt.String, dflt.Valid, c.want)
			}
		})
	}
}

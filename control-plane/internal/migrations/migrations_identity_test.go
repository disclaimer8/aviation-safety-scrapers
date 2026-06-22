package migrations

// These tests drive the Task 2 fixes required by section 17.5.A of
// docs/superpowers/specs/2026-06-22-coverage-control-plane-foundation-design.md
// ("Harden migration identity and ordering"). They are expected to FAIL against
// the current runner, which sorts lexically, accepts arbitrary version widths,
// and skips an already-recorded version without verifying its stored name or
// content.
//
// Canonical filename interpretation used here follows the spec example
// `001_name.sql`: a zero-padded three-digit version, an underscore, then a name.
//
// Not covered here (deferred to the implementer per 17.5.A):
//   - numeric ordering: structurally guaranteed once canonical fixed-width
//     naming is enforced (lexical order == numeric order), so it is exercised by
//     TestApplyRejectsNoncanonicalMigrationFilename rather than a separate case;
//   - concurrent migration execution: needs the version check inside the apply
//     transaction; better verified as an integration test than a flaky unit test.

import (
	"context"
	"sync"
	"testing"
	"testing/fstest"
)

// 17.5.A: noncanonical filenames must be rejected. The current runner accepts
// any Atoi-able width (e.g. "1_core.sql"), which makes "1_core.sql" and
// "001_core.sql" indistinguishable and defeats duplicate detection.
func TestApplyRejectsNoncanonicalMigrationFilename(t *testing.T) {
	db := openTestDB(t)
	migrationFS := fstest.MapFS{
		"sql/1_core.sql": {Data: []byte(`CREATE TABLE probe (id INTEGER PRIMARY KEY)`)},
	}

	if err := applyFS(context.Background(), db, migrationFS); err == nil {
		t.Fatal("expected noncanonical filename 1_core.sql to be rejected, got nil")
	}
}

// 17.5.A: two files that resolve to the same version must be rejected before any
// migration is applied. The current runner applies the first lexically and
// silently skips the second because the version already exists.
func TestApplyRejectsDuplicateMigrationVersion(t *testing.T) {
	db := openTestDB(t)
	migrationFS := fstest.MapFS{
		"sql/001_alpha.sql": {Data: []byte(`CREATE TABLE alpha (id INTEGER PRIMARY KEY)`)},
		"sql/001_beta.sql":  {Data: []byte(`CREATE TABLE beta (id INTEGER PRIMARY KEY)`)},
	}

	if err := applyFS(context.Background(), db, migrationFS); err == nil {
		t.Fatal("expected duplicate version 001 to be rejected, got nil")
	}
}

// 17.5.A: an already-applied version whose recorded name no longer matches the
// embedded file is migration drift and must fail clearly. The current runner
// skips the version silently.
func TestApplyDetectsChangedMigrationName(t *testing.T) {
	db := openTestDB(t)
	ctx := context.Background()

	original := fstest.MapFS{
		"sql/001_alpha.sql": {Data: []byte(`CREATE TABLE alpha (id INTEGER PRIMARY KEY)`)},
	}
	if err := applyFS(ctx, db, original); err != nil {
		t.Fatalf("first apply: %v", err)
	}

	renamed := fstest.MapFS{
		"sql/001_beta.sql": {Data: []byte(`CREATE TABLE alpha (id INTEGER PRIMARY KEY)`)},
	}
	if err := applyFS(ctx, db, renamed); err == nil {
		t.Fatal("expected changed migration name for version 001 to be rejected, got nil")
	}
}

// 17.5.A: an already-applied version whose content changed is migration drift
// and must fail clearly. This requires storing and verifying a checksum in
// schema_migrations; the current runner has no checksum column and skips the
// version silently.
func TestApplyDetectsChangedMigrationChecksum(t *testing.T) {
	db := openTestDB(t)
	ctx := context.Background()

	original := fstest.MapFS{
		"sql/001_core.sql": {Data: []byte(`CREATE TABLE probe (id INTEGER PRIMARY KEY)`)},
	}
	if err := applyFS(ctx, db, original); err != nil {
		t.Fatalf("first apply: %v", err)
	}

	mutated := fstest.MapFS{
		"sql/001_core.sql": {Data: []byte(`CREATE TABLE probe (id INTEGER PRIMARY KEY, extra TEXT)`)},
	}
	if err := applyFS(ctx, db, mutated); err == nil {
		t.Fatal("expected changed migration checksum for version 001 to be rejected, got nil")
	}
}

// 17.5.A: migrations must apply in numeric version order regardless of the order
// fs.Glob happens to return them. A later migration may depend on an earlier
// one's table; applying out of order would fail.
func TestApplyOrdersMigrationsByNumericVersion(t *testing.T) {
	db := openTestDB(t)
	ctx := context.Background()

	// 002 references the table created by 001; if order were not numeric this
	// would fail with "no such table".
	migrationFS := fstest.MapFS{
		"sql/002_child.sql":  {Data: []byte(`INSERT INTO parent(id) VALUES (1)`)},
		"sql/001_parent.sql": {Data: []byte(`CREATE TABLE parent (id INTEGER PRIMARY KEY)`)},
	}
	if err := applyFS(ctx, db, migrationFS); err != nil {
		t.Fatalf("apply in numeric order: %v", err)
	}

	rows, err := db.QueryContext(ctx,
		`SELECT version FROM schema_migrations ORDER BY rowid`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()

	var order []int
	for rows.Next() {
		var v int
		if err := rows.Scan(&v); err != nil {
			t.Fatal(err)
		}
		order = append(order, v)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	if len(order) != 2 || order[0] != 1 || order[1] != 2 {
		t.Fatalf("applied order=%v, want [1 2]", order)
	}
}

// 17.5.A: the applied-version check lives inside the migration transaction, so
// two runners against the same database serialize instead of racing a
// read-then-apply gap. Each migration must still be applied exactly once.
func TestApplyIsConcurrencySafe(t *testing.T) {
	db := openTestDB(t)
	ctx := context.Background()
	migrationFS := fstest.MapFS{
		"sql/001_probe.sql": {Data: []byte(`CREATE TABLE probe (id INTEGER PRIMARY KEY)`)},
	}

	var wg sync.WaitGroup
	errs := make([]error, 4)
	for i := range errs {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			errs[i] = applyFS(ctx, db, migrationFS)
		}(i)
	}
	wg.Wait()

	for i, err := range errs {
		if err != nil {
			t.Fatalf("concurrent applyFS[%d]: %v", i, err)
		}
	}

	var count int
	if err := db.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM schema_migrations WHERE version = 1`).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 1 {
		t.Fatalf("schema_migrations rows for version 1 = %d, want 1", count)
	}
}

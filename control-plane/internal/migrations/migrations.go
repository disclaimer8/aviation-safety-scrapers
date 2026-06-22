package migrations

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"embed"
	"encoding/hex"
	"fmt"
	"io/fs"
	"regexp"
	"sort"
	"strconv"
)

//go:embed sql/*.sql
var migrationFiles embed.FS

// canonicalName matches a zero-padded three-digit version, an underscore, and a
// lowercase snake-case name, e.g. "sql/001_core.sql". Enforcing a fixed numeric
// width makes lexical and numeric ordering equivalent and lets us reliably
// detect duplicate versions across differing filenames.
var canonicalName = regexp.MustCompile(`^sql/(\d{3})_([a-z0-9]+(?:_[a-z0-9]+)*)\.sql$`)

type migration struct {
	version  int
	name     string
	body     string
	checksum string
}

func Apply(ctx context.Context, db *sql.DB) error {
	return applyFS(ctx, db, migrationFiles)
}

func applyFS(ctx context.Context, db *sql.DB, migrationFS fs.FS) error {
	if _, err := db.ExecContext(ctx, `CREATE TABLE IF NOT EXISTS schema_migrations (
		version INTEGER PRIMARY KEY,
		name TEXT NOT NULL,
		checksum TEXT NOT NULL,
		applied_at INTEGER NOT NULL
	) STRICT`); err != nil {
		return fmt.Errorf("create schema_migrations: %w", err)
	}

	migrationsList, err := loadMigrations(migrationFS)
	if err != nil {
		return err
	}

	for _, m := range migrationsList {
		if err := applyOne(ctx, db, m); err != nil {
			return err
		}
	}
	return nil
}

// loadMigrations discovers, validates, and orders the embedded migrations. It
// rejects noncanonical filenames and duplicate versions before any migration is
// applied.
func loadMigrations(migrationFS fs.FS) ([]migration, error) {
	names, err := fs.Glob(migrationFS, "sql/*.sql")
	if err != nil {
		return nil, fmt.Errorf("discover migrations: %w", err)
	}

	seen := make(map[int]string, len(names))
	out := make([]migration, 0, len(names))
	for _, path := range names {
		m := canonicalName.FindStringSubmatch(path)
		if m == nil {
			return nil, fmt.Errorf("noncanonical migration filename %q (want NNN_name.sql)", path)
		}
		version, err := strconv.Atoi(m[1])
		if err != nil || version < 1 {
			return nil, fmt.Errorf("invalid migration version in %q", path)
		}
		if prev, ok := seen[version]; ok {
			return nil, fmt.Errorf("duplicate migration version %d (%q and %q)", version, prev, path)
		}
		seen[version] = path

		body, err := fs.ReadFile(migrationFS, path)
		if err != nil {
			return nil, fmt.Errorf("read migration %s: %w", path, err)
		}
		sum := sha256.Sum256(body)
		out = append(out, migration{
			version:  version,
			name:     m[1] + "_" + m[2],
			body:     string(body),
			checksum: hex.EncodeToString(sum[:]),
		})
	}

	sort.Slice(out, func(i, j int) bool { return out[i].version < out[j].version })
	return out, nil
}

// applyOne applies a single migration inside one transaction. The applied-version
// check happens inside the transaction so that concurrent migration runners
// serialize on the write transaction rather than racing a read-then-apply gap.
// Already-applied migrations are verified against their stored name and checksum;
// any drift fails clearly instead of being silently skipped.
func applyOne(ctx context.Context, db *sql.DB, m migration) error {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("begin transaction: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	var storedName, storedChecksum string
	err = tx.QueryRowContext(ctx,
		`SELECT name, checksum FROM schema_migrations WHERE version = ?`,
		m.version,
	).Scan(&storedName, &storedChecksum)
	switch {
	case err == nil:
		// Already applied: verify identity and content match.
		if storedName != m.name {
			return fmt.Errorf("migration %03d name drift: recorded %q, embedded %q",
				m.version, storedName, m.name)
		}
		if storedChecksum != m.checksum {
			return fmt.Errorf("migration %03d checksum drift: recorded %s, embedded %s",
				m.version, storedChecksum, m.checksum)
		}
		return nil
	case err == sql.ErrNoRows:
		// Not yet applied: fall through and apply.
	default:
		return fmt.Errorf("check migration %03d: %w", m.version, err)
	}

	if _, err := tx.ExecContext(ctx, m.body); err != nil {
		return fmt.Errorf("apply migration %03d_%s: %w", m.version, m.name, err)
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO schema_migrations(version, name, checksum, applied_at)
		VALUES (?, ?, ?, CAST(unixepoch('subsec') * 1000 AS INTEGER))
	`, m.version, m.name, m.checksum); err != nil {
		return fmt.Errorf("record migration %03d: %w", m.version, err)
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("commit migration %03d: %w", m.version, err)
	}
	return nil
}

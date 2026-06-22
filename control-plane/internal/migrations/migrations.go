package migrations

import (
	"context"
	"database/sql"
	"embed"
	"fmt"
	"io/fs"
	"sort"
	"strconv"
	"strings"
)

//go:embed sql/*.sql
var migrationFiles embed.FS

func Apply(ctx context.Context, db *sql.DB) error {
	return applyFS(ctx, db, migrationFiles)
}

func applyFS(ctx context.Context, db *sql.DB, migrationFS fs.FS) error {
	if _, err := db.ExecContext(ctx, `CREATE TABLE IF NOT EXISTS schema_migrations (
		version INTEGER PRIMARY KEY,
		name TEXT NOT NULL,
		applied_at INTEGER NOT NULL
	)`); err != nil {
		return fmt.Errorf("create schema_migrations: %w", err)
	}

	names, err := fs.Glob(migrationFS, "sql/*.sql")
	if err != nil {
		return fmt.Errorf("discover migrations: %w", err)
	}
	sort.Strings(names)

	for _, name := range names {
		version, migrationName, err := parseName(name)
		if err != nil {
			return err
		}

		var applied bool
		if err := db.QueryRowContext(ctx,
			`SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE version = ?)`,
			version,
		).Scan(&applied); err != nil {
			return fmt.Errorf("check migration %s: %w", name, err)
		}
		if applied {
			continue
		}

		body, err := fs.ReadFile(migrationFS, name)
		if err != nil {
			return fmt.Errorf("read migration %s: %w", name, err)
		}
		if err := applyOne(ctx, db, version, migrationName, string(body)); err != nil {
			return fmt.Errorf("apply migration %s: %w", name, err)
		}
	}

	return nil
}

func parseName(path string) (int, string, error) {
	name := strings.TrimSuffix(strings.TrimPrefix(path, "sql/"), ".sql")
	parts := strings.SplitN(name, "_", 2)
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		return 0, "", fmt.Errorf("invalid migration filename %q", path)
	}

	version, err := strconv.Atoi(parts[0])
	if err != nil || version < 1 {
		return 0, "", fmt.Errorf("invalid migration version in %q", path)
	}
	return version, name, nil
}

func applyOne(
	ctx context.Context,
	db *sql.DB,
	version int,
	name string,
	body string,
) error {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("begin transaction: %w", err)
	}

	if _, err := tx.ExecContext(ctx, body); err != nil {
		_ = tx.Rollback()
		return fmt.Errorf("execute SQL: %w", err)
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO schema_migrations(version, name, applied_at)
		VALUES (?, ?, CAST(unixepoch('subsec') * 1000 AS INTEGER))
	`, version, name); err != nil {
		_ = tx.Rollback()
		return fmt.Errorf("record version: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("commit transaction: %w", err)
	}
	return nil
}

package database

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

func TestOpenEnablesForeignKeysAndWAL(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "coverage.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	var foreignKeys int
	if err := db.QueryRowContext(context.Background(), "PRAGMA foreign_keys").Scan(&foreignKeys); err != nil {
		t.Fatal(err)
	}
	if foreignKeys != 1 {
		t.Fatalf("foreign_keys=%d, want 1", foreignKeys)
	}

	var mode string
	if err := db.QueryRowContext(context.Background(), "PRAGMA journal_mode").Scan(&mode); err != nil {
		t.Fatal(err)
	}
	if mode != "wal" {
		t.Fatalf("journal_mode=%q, want wal", mode)
	}

	var busyTimeout int
	if err := db.QueryRowContext(context.Background(), "PRAGMA busy_timeout").Scan(&busyTimeout); err != nil {
		t.Fatal(err)
	}
	if busyTimeout != 10000 {
		t.Fatalf("busy_timeout=%d, want 10000", busyTimeout)
	}
}

func TestOpenEscapesReservedPathCharacters(t *testing.T) {
	path := filepath.Join(t.TempDir(), "coverage?#%.db")

	db, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("CREATE TABLE reserved_path_probe (id INTEGER PRIMARY KEY)"); err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}

	if _, err := os.Stat(path); err != nil {
		t.Fatalf("stat intended database path: %v", err)
	}

	db, err = Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	var tables int
	if err := db.QueryRow(`
		SELECT count(*)
		FROM sqlite_master
		WHERE type = 'table' AND name = 'reserved_path_probe'
	`).Scan(&tables); err != nil {
		t.Fatal(err)
	}
	if tables != 1 {
		t.Fatalf("reserved_path_probe tables=%d, want 1", tables)
	}
}

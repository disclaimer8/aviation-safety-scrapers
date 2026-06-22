package database

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/config"
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
}

func TestDefaultHTTP(t *testing.T) {
	got := config.DefaultHTTP()

	if got.UserAgent != "aviation-coverage-control-plane/1.0 (+https://github.com/denyskolomiiets/aviation-safety-scrapers)" {
		t.Fatalf("UserAgent=%q", got.UserAgent)
	}
	if got.Timeout != 30*time.Second {
		t.Fatalf("Timeout=%s, want 30s", got.Timeout)
	}
	if got.MaxBytes != 8<<20 {
		t.Fatalf("MaxBytes=%d, want %d", got.MaxBytes, 8<<20)
	}
	if got.Retries != 2 {
		t.Fatalf("Retries=%d, want 2", got.Retries)
	}
	if config.DefaultAIAURL != "https://www.icao.int/safety/AIG/AIA" {
		t.Fatalf("DefaultAIAURL=%q", config.DefaultAIAURL)
	}
	if config.DefaultRAIOURL != "https://www.icao.int/safety/regional-safety-cooperation/List-of-RAIOs-and-ICMs" {
		t.Fatalf("DefaultRAIOURL=%q", config.DefaultRAIOURL)
	}
}

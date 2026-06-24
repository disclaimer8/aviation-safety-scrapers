package app

import (
	"bytes"
	"context"
	"io"
	"os"
	"strings"
	"testing"
)

func TestProcessManufacturerRequiresDB(t *testing.T) {
	var out, errb bytes.Buffer
	if code := Run(context.Background(), []string{"process-manufacturer"}, &out, &errb); code != 2 {
		t.Fatalf("exit = %d, want 2", code)
	}
}

func TestProcessManufacturerWithFixture(t *testing.T) {
	// Copy the testdata fixture to a temp path so we don't depend on a relative path.
	fixture := "../../internal/worker/manufacturer/testdata/safetyfirst_listing.html"
	fixtureData, err := os.ReadFile(fixture)
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}
	tmp := t.TempDir()
	fixtureCopy := tmp + "/listing.html"
	if err := os.WriteFile(fixtureCopy, fixtureData, 0o600); err != nil {
		t.Fatalf("write fixture copy: %v", err)
	}

	dbPath := tmp + "/coverage.db"
	ctx := context.Background()
	var out, errb bytes.Buffer

	// Migrate first.
	if code := Run(ctx, []string{"migrate", "--db", dbPath}, &out, &errb); code != 0 {
		t.Fatalf("migrate exit %d: %s", code, errb.String())
	}
	errb.Reset()

	// Run process-manufacturer with --source-file so no network is needed.
	args := []string{"process-manufacturer", "--db", dbPath, "--source-file", fixtureCopy}
	if code := Run(ctx, args, io.Discard, &errb); code != 0 {
		t.Fatalf("process-manufacturer exit %d: %s", code, errb.String())
	}

	got := errb.String()
	if !strings.Contains(got, "staged=") {
		t.Fatalf("stderr %q does not contain 'staged='", got)
	}
}

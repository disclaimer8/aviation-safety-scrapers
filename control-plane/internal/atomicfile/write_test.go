package atomicfile_test

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/atomicfile"
)

// TestWriteAtomicallyReplacesDestination seeds an existing file with "old",
// writes new JSON content, decodes it back, and verifies no temp file remains.
func TestWriteAtomicallyReplacesDestination(t *testing.T) {
	t.Helper()
	dir := t.TempDir()
	dst := filepath.Join(dir, "output.json")

	// Seed destination with existing content.
	if err := os.WriteFile(dst, []byte(`"old"`), 0644); err != nil {
		t.Fatal(err)
	}

	payload := map[string]string{"hello": "world"}
	data, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}

	if err := atomicfile.Write(dst, data); err != nil {
		t.Fatalf("Write: %v", err)
	}

	// Verify destination contains the new content.
	raw, err := os.ReadFile(dst)
	if err != nil {
		t.Fatalf("read dst: %v", err)
	}
	var got map[string]string
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got["hello"] != "world" {
		t.Fatalf("unexpected content: %v", got)
	}

	// Assert no temp file remains.
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		if e.Name() != "output.json" {
			t.Errorf("unexpected file after Write: %s", e.Name())
		}
	}
}

// TestWriteNoTempOnSuccess verifies that a clean write leaves exactly one file.
func TestWriteNoTempOnSuccess(t *testing.T) {
	dir := t.TempDir()
	dst := filepath.Join(dir, "out.json")

	if err := atomicfile.Write(dst, []byte(`{"ok":true}`)); err != nil {
		t.Fatal(err)
	}

	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 {
		t.Errorf("expected 1 file, got %d", len(entries))
	}
}

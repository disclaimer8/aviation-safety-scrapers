// Package atomicfile provides an atomic write helper that replaces the
// destination file without leaving a partial write visible to readers.
// The temporary file is created in the same directory as the destination so
// that os.Rename is a same-filesystem rename (not a cross-device copy).
package atomicfile

import (
	"fmt"
	"os"
	"path/filepath"
)

// Write writes data to a temporary file in the same directory as path, syncs
// and closes it, sets permissions to 0644, then atomically renames it over
// path. On every error path the temporary file is removed before returning.
func Write(path string, data []byte) error {
	dir := filepath.Dir(path)

	tmp, err := os.CreateTemp(dir, ".atomicwrite-*")
	if err != nil {
		return fmt.Errorf("atomicfile: create temp: %w", err)
	}
	tmpName := tmp.Name()

	// Ensure temp is cleaned up on every error path.
	cleanup := func() {
		_ = os.Remove(tmpName)
	}

	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		cleanup()
		return fmt.Errorf("atomicfile: write temp: %w", err)
	}

	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		cleanup()
		return fmt.Errorf("atomicfile: sync temp: %w", err)
	}

	if err := tmp.Close(); err != nil {
		cleanup()
		return fmt.Errorf("atomicfile: close temp: %w", err)
	}

	if err := os.Chmod(tmpName, 0644); err != nil {
		cleanup()
		return fmt.Errorf("atomicfile: chmod temp: %w", err)
	}

	if err := os.Rename(tmpName, path); err != nil {
		cleanup()
		return fmt.Errorf("atomicfile: rename temp to %s: %w", path, err)
	}

	return nil
}

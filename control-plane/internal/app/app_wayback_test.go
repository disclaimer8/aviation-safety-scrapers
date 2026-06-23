package app

import (
	"bytes"
	"context"
	"testing"
)

func TestProcessWaybackRequiresDB(t *testing.T) {
	var out, errb bytes.Buffer
	code := Run(context.Background(), []string{"process-wayback"}, &out, &errb)
	if code != 2 { // exitUsage
		t.Fatalf("exit = %d, want 2 (usage)", code)
	}
}

func TestProcessWaybackEmptyQueueOK(t *testing.T) {
	// A migrated+seeded DB with no pending wayback jobs: command succeeds,
	// processes zero. (No network is touched because there are no jobs to run.)
	dir := t.TempDir()
	path := dir + "/coverage.db"
	ctx := context.Background()
	var out, errb bytes.Buffer
	if code := Run(ctx, []string{"migrate", "--db", path}, &out, &errb); code != 0 {
		t.Fatalf("migrate exit %d: %s", code, errb.String())
	}
	// Do NOT seed/enqueue — empty queue keeps this test offline.
	errb.Reset()
	code := Run(ctx, []string{"process-wayback", "--db", path}, &out, &errb)
	if code != 0 {
		t.Fatalf("process-wayback exit %d: %s", code, errb.String())
	}
}

package app

import (
	"bytes"
	"context"
	"testing"
)

func TestProcessRegionalRequiresDB(t *testing.T) {
	var out, errb bytes.Buffer
	if code := Run(context.Background(), []string{"process-regional"}, &out, &errb); code != 2 {
		t.Fatalf("exit = %d, want 2", code)
	}
}

func TestProcessRegionalEmptyQueueOK(t *testing.T) {
	dir := t.TempDir()
	path := dir + "/coverage.db"
	ctx := context.Background()
	var out, errb bytes.Buffer
	if code := Run(ctx, []string{"migrate", "--db", path}, &out, &errb); code != 0 {
		t.Fatalf("migrate exit %d: %s", code, errb.String())
	}
	errb.Reset()
	if code := Run(ctx, []string{"process-regional", "--db", path}, &out, &errb); code != 0 {
		t.Fatalf("process-regional exit %d: %s", code, errb.String())
	}
}

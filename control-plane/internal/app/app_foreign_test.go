package app

import (
	"bytes"
	"context"
	"testing"
)

func TestProcessForeignRequiresDB(t *testing.T) {
	var out, errb bytes.Buffer
	if code := Run(context.Background(), []string{"process-foreign-search"}, &out, &errb); code != 2 {
		t.Fatalf("exit = %d, want 2 (usage)", code)
	}
}

func TestProcessForeignEmptyQueueOK(t *testing.T) {
	dir := t.TempDir()
	path := dir + "/coverage.db"
	ctx := context.Background()
	var out, errb bytes.Buffer
	if code := Run(ctx, []string{"migrate", "--db", path}, &out, &errb); code != 0 {
		t.Fatalf("migrate exit %d: %s", code, errb.String())
	}
	errb.Reset()
	if code := Run(ctx, []string{"process-foreign-search", "--db", path}, &out, &errb); code != 0 {
		t.Fatalf("process-foreign-search exit %d: %s", code, errb.String())
	}
}

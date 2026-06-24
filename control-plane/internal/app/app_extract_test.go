package app

import (
	"bytes"
	"context"
	"strings"
	"testing"
)

func TestProcessExtractRequiresDB(t *testing.T) {
	var out, errb bytes.Buffer
	if code := Run(context.Background(), []string{"process-extract"}, &out, &errb); code != 2 {
		t.Fatalf("exit = %d, want 2 (missing --db)", code)
	}
	if !strings.Contains(errb.String(), "--db is required") {
		t.Fatalf("stderr=%q", errb.String())
	}
}

func TestProcessExtractEmptyQueueOK(t *testing.T) {
	dir := t.TempDir()
	path := dir + "/coverage.db"
	ctx := context.Background()
	var out, errb bytes.Buffer
	if code := Run(ctx, []string{"migrate", "--db", path}, &out, &errb); code != 0 {
		t.Fatalf("migrate exit %d: %s", code, errb.String())
	}
	errb.Reset()
	if code := Run(ctx, []string{"process-extract", "--db", path}, &out, &errb); code != 0 {
		t.Fatalf("process-extract exit %d: %s", code, errb.String())
	}
	got := errb.String()
	if !strings.Contains(got, "extracted=") || !strings.Contains(got, "skipped=") || !strings.Contains(got, "failed=") {
		t.Fatalf("stderr missing stats: %q", got)
	}
}

func TestProcessWaybackExtractAliasOK(t *testing.T) {
	dir := t.TempDir()
	path := dir + "/coverage.db"
	ctx := context.Background()
	var out, errb bytes.Buffer
	if code := Run(ctx, []string{"migrate", "--db", path}, &out, &errb); code != 0 {
		t.Fatalf("migrate exit %d: %s", code, errb.String())
	}
	errb.Reset()
	if code := Run(ctx, []string{"process-wayback-extract", "--db", path}, &out, &errb); code != 0 {
		t.Fatalf("process-wayback-extract exit %d: %s", code, errb.String())
	}
	got := errb.String()
	if !strings.Contains(got, "deprecated") {
		t.Fatalf("stderr missing deprecation note: %q", got)
	}
}

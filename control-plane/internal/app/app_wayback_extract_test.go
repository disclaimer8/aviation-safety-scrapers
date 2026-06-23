package app

import (
	"bytes"
	"context"
	"strings"
	"testing"
)

func TestRunProcessWaybackExtractRequiresDB(t *testing.T) {
	var stderr bytes.Buffer
	code := Run(context.Background(), []string{"process-wayback-extract"}, &bytes.Buffer{}, &stderr)
	if code == 0 {
		t.Fatal("expected non-zero exit without --db")
	}
	if !strings.Contains(stderr.String(), "--db is required") {
		t.Fatalf("stderr=%q", stderr.String())
	}
}

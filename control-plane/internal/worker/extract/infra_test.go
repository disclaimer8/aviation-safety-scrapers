package extract

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/url"
	"syscall"
	"testing"
)

// dialRefusedErr builds an error shaped like what net/http actually returns for
// a connection-refused dial failure (the confirmed production symptom: "dial
// tcp 127.0.0.1:11434: connect: connection refused"), wrapped the way
// wayback/llm.go and wayback/ocr.go wrap client.Do errors with fmt.Errorf.
func dialRefusedErr() error {
	opErr := &net.OpError{Op: "dial", Net: "tcp", Err: syscall.ECONNREFUSED}
	urlErr := &url.Error{Op: "Post", URL: "http://127.0.0.1:11434/api/generate", Err: opErr}
	return fmt.Errorf("wayback: llm post: %w", urlErr)
}

func TestIsInfraErrorDetectsWrappedDialRefused(t *testing.T) {
	if !isInfraError(dialRefusedErr()) {
		t.Fatal("expected isInfraError=true for a wrapped dial-refused error (the confirmed production shape)")
	}
}

func TestIsInfraErrorDetectsBareNetOpError(t *testing.T) {
	err := &net.OpError{Op: "dial", Net: "tcp", Err: errors.New("connection refused")}
	if !isInfraError(err) {
		t.Fatal("expected isInfraError=true for a bare *net.OpError")
	}
}

func TestIsInfraErrorRejectsApplicationError(t *testing.T) {
	// A non-200 status or bad response body is a property of the request/
	// document, not the endpoint's availability — must NOT be classified infra.
	err := fmt.Errorf("wayback: llm status %d: %s", 500, "internal error")
	if isInfraError(err) {
		t.Fatal("expected isInfraError=false for a plain application-level error")
	}
}

func TestIsInfraErrorRejectsNil(t *testing.T) {
	if isInfraError(nil) {
		t.Fatal("nil must not be classified as an infra error")
	}
}

// ─── extractOne / ProcessExtractPending abort behavior ──────────────────────

// TestExtractOneAbortsOnOCRInfraErrorWithoutIncrementingAttempts is the core
// GO-CP-3 regression test: a dial-refused error talking to the OCR endpoint
// must NOT call RecordFailure (extraction_attempts stays untouched,
// extraction_status stays 'pending') and must return an *InfraAbortError
// instead of a terminal "failed" status.
func TestExtractOneAbortsOnOCRInfraErrorWithoutIncrementingAttempts(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID, _ := seedDownloadedDoc(t, db, "KE", "k1")
	writePDF(t, db, docID)
	doc := loadDoc(t, db, docID)

	status, err := extractOne(ctx, db, WaybackSource{}, &fixtureOCRClient{Err: dialRefusedErr()}, &fixtureLLMClient{}, t.TempDir(), doc)
	if status != "" {
		t.Fatalf("status=%q want \"\" (an infra abort is not a terminal per-document status)", status)
	}
	var infraErr *InfraAbortError
	if !errors.As(err, &infraErr) {
		t.Fatalf("expected *InfraAbortError, got %v (%T)", err, err)
	}
	if infraErr.Step != "ocr" {
		t.Fatalf("Step=%q want %q", infraErr.Step, "ocr")
	}
	if infraErr.DocID != docID {
		t.Fatalf("DocID=%d want %d", infraErr.DocID, docID)
	}

	var attempts int
	var estatus string
	if err := db.QueryRowContext(ctx,
		`SELECT extraction_attempts, extraction_status FROM staged_wayback_documents WHERE id=?`, docID).
		Scan(&attempts, &estatus); err != nil {
		t.Fatal(err)
	}
	if attempts != 0 {
		t.Fatalf("extraction_attempts=%d want 0 — an infra outage must not burn the document's retry budget", attempts)
	}
	if estatus != "pending" {
		t.Fatalf("extraction_status=%q want unchanged (pending) — document did nothing wrong", estatus)
	}
}

// TestExtractOneAbortsOnLLMInfraErrorWithoutIncrementingAttempts is the LLM-step
// counterpart: OCR succeeds, then a dial-refused error talking to the LLM
// endpoint (the exact production symptom — Ollama tunnel down) must abort the
// same way.
func TestExtractOneAbortsOnLLMInfraErrorWithoutIncrementingAttempts(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID, _ := seedDownloadedDoc(t, db, "KE", "k1")
	writePDF(t, db, docID)
	doc := loadDoc(t, db, docID)

	status, err := extractOne(ctx, db, WaybackSource{}, &fixtureOCRClient{Text: "REPORT"}, &fixtureLLMClient{Err: dialRefusedErr()}, t.TempDir(), doc)
	if status != "" {
		t.Fatalf("status=%q want \"\"", status)
	}
	var infraErr *InfraAbortError
	if !errors.As(err, &infraErr) {
		t.Fatalf("expected *InfraAbortError, got %v (%T)", err, err)
	}
	if infraErr.Step != "llm" {
		t.Fatalf("Step=%q want %q", infraErr.Step, "llm")
	}

	var attempts int
	if err := db.QueryRowContext(ctx,
		`SELECT extraction_attempts FROM staged_wayback_documents WHERE id=?`, docID).Scan(&attempts); err != nil {
		t.Fatal(err)
	}
	if attempts != 0 {
		t.Fatalf("extraction_attempts=%d want 0", attempts)
	}
}

// TestProcessExtractPendingAbortsBatchOnInfraError proves the batch-level
// contract: with two pending documents and an LLM endpoint that is down, the
// FIRST document's infra error must stop the pass immediately — the second
// document must be left completely untouched (not even attempted), because it
// would fail identically against the same dead endpoint.
func TestProcessExtractPendingAbortsBatchOnInfraError(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	doc1ID, _ := seedDownloadedDoc(t, db, "KE", "k1")
	doc2ID, _ := seedDownloadedDoc(t, db, "TZ", "k2")
	writePDF(t, db, doc1ID)
	writePDF(t, db, doc2ID)
	store := t.TempDir()

	stats, err := ProcessExtractPending(ctx, db, &fixtureOCRClient{Text: "REPORT"}, &fixtureLLMClient{Err: dialRefusedErr()}, store, 0, WaybackSource{})
	var infraErr *InfraAbortError
	if !errors.As(err, &infraErr) {
		t.Fatalf("expected *InfraAbortError from ProcessExtractPending, got %v", err)
	}
	if stats.Extracted != 0 || stats.Skipped != 0 || stats.Failed != 0 {
		t.Fatalf("stats=%+v want all zero (abort must happen before any document is counted terminal)", stats)
	}

	// Neither document may have had its attempt counter touched. The first
	// document legitimately progresses to 'ocr_done' (OCR succeeded — only the
	// LLM call, further down the pipeline, hit the dead endpoint); genuine
	// progress like that is preserved, it just isn't a 'failed' terminal state.
	// The second document must be completely untouched — the abort happens
	// before it is even attempted.
	var attempts1 int
	var status1 string
	db.QueryRowContext(ctx, `SELECT extraction_attempts, extraction_status FROM staged_wayback_documents WHERE id=?`, doc1ID).
		Scan(&attempts1, &status1)
	if attempts1 != 0 || status1 != "ocr_done" {
		t.Errorf("doc1 (%d): attempts=%d status=%q, want 0/ocr_done", doc1ID, attempts1, status1)
	}
	var attempts2 int
	var status2 string
	db.QueryRowContext(ctx, `SELECT extraction_attempts, extraction_status FROM staged_wayback_documents WHERE id=?`, doc2ID).
		Scan(&attempts2, &status2)
	if attempts2 != 0 || status2 != "pending" {
		t.Errorf("doc2 (%d): attempts=%d status=%q, want 0/pending (never attempted)", doc2ID, attempts2, status2)
	}
}

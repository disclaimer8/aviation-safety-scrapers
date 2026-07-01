package extract

import (
	"context"
	"testing"
)

func TestResetFailedResetsMatchingRowsOnly(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docA, _ := seedDownloadedDoc(t, db, "KE", "a1")
	docB, _ := seedDownloadedDoc(t, db, "TZ", "b1")

	// docA: failed by the infra outage (connection refused), attempts burned
	// to 3 by the pre-fix code path — exactly the state ResetFailed exists to
	// recover from.
	if _, err := db.ExecContext(ctx, `
		UPDATE staged_wayback_documents
		   SET extraction_status='failed', extraction_attempts=3,
		       extraction_error='wayback: llm post: dial tcp 127.0.0.1:11434: connect: connection refused'
		 WHERE id=?`, docA); err != nil {
		t.Fatal(err)
	}
	// docB: failed for an unrelated, genuine document-level reason — must NOT
	// be touched by a reset scoped to the connection-refused pattern.
	if _, err := db.ExecContext(ctx, `
		UPDATE staged_wayback_documents
		   SET extraction_status='failed', extraction_attempts=2,
		       extraction_error='wayback: llm status 500: internal error'
		 WHERE id=?`, docB); err != nil {
		t.Fatal(err)
	}

	n, err := ResetFailed(ctx, db, "wayback", "%connection refused%")
	if err != nil {
		t.Fatalf("ResetFailed: %v", err)
	}
	if n != 1 {
		t.Fatalf("reset count=%d want 1", n)
	}

	var statusA string
	var attemptsA int
	db.QueryRowContext(ctx, `SELECT extraction_status, extraction_attempts FROM staged_wayback_documents WHERE id=?`, docA).
		Scan(&statusA, &attemptsA)
	if statusA != "pending" || attemptsA != 0 {
		t.Fatalf("docA status=%q attempts=%d, want pending/0", statusA, attemptsA)
	}

	var statusB string
	var attemptsB int
	db.QueryRowContext(ctx, `SELECT extraction_status, extraction_attempts FROM staged_wayback_documents WHERE id=?`, docB).
		Scan(&statusB, &attemptsB)
	if statusB != "failed" || attemptsB != 2 {
		t.Fatalf("docB (unrelated failure) status=%q attempts=%d, want unchanged failed/2", statusB, attemptsB)
	}
}

func TestResetFailedUnknownStoreErrors(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	if _, err := ResetFailed(ctx, db, "not-a-real-store", "%x%"); err == nil {
		t.Fatal("expected an error for an unknown store name")
	}
}

func TestResetFailedScopedPerStore(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	docID, _ := seedRegionalDoc(t, db, "TZ", "ECCAA", "https://eccaa.example/001.pdf")
	if _, err := db.ExecContext(ctx, `
		UPDATE staged_regional_documents
		   SET extraction_status='failed', extraction_attempts=3,
		       extraction_error='regional: fetch html: dial tcp: connect: connection refused'
		 WHERE id=?`, docID); err != nil {
		t.Fatal(err)
	}

	// Resetting the "wayback" store must not touch the regional row.
	n, err := ResetFailed(ctx, db, "wayback", "%connection refused%")
	if err != nil {
		t.Fatal(err)
	}
	if n != 0 {
		t.Fatalf("wayback reset count=%d want 0 (regional row must not be touched)", n)
	}

	n, err = ResetFailed(ctx, db, "regional", "%connection refused%")
	if err != nil {
		t.Fatal(err)
	}
	if n != 1 {
		t.Fatalf("regional reset count=%d want 1", n)
	}
}

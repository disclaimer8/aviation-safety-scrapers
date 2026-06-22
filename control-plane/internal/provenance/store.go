// Package provenance records immutable source snapshots and tracks import run
// lifecycle. All mutations go through this package so that the rest of the
// codebase never bypasses the INSERT-only invariant on source_snapshots.
package provenance

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"fmt"
	"time"
)

// DBTX is satisfied by both *sql.DB and *sql.Tx, allowing callers in Tasks
// 9/10 to call these functions inside an import transaction without changing
// the function signatures.
type DBTX interface {
	ExecContext(context.Context, string, ...any) (sql.Result, error)
	QueryRowContext(context.Context, string, ...any) *sql.Row
}

// SnapshotInput carries the HTTP response fields that are stored as a snapshot.
// Body is hashed with SHA-256; the resulting checksum drives idempotency.
type SnapshotInput struct {
	SourceID     int64
	SourceURL    string
	FinalURL     string
	StatusCode   int
	ContentType  string
	ETag         string
	LastModified string
	FetchedAt    time.Time
	Body         []byte
}

// Snapshot mirrors the source_snapshots row returned after a put.
type Snapshot struct {
	ID           int64
	SourceID     int64
	SourceURL    string
	FinalURL     string
	StatusCode   int
	ContentType  string
	ETag         string
	LastModified string
	FetchedAt    time.Time
	Checksum     string
	SizeBytes    int
}

// Run mirrors a single import_runs row.
type Run struct {
	ID        int64
	Importer  string
	SourceURL string
	StartedAt time.Time
	Status    string
}

// RunResult carries the final counts and status to write when a run finishes.
type RunResult struct {
	Status       string // running | success | partial | failed | unchanged
	Parsed       int
	Applied      int
	Warnings     int
	Conflicts    int
	ErrorSummary string
}

// PutSnapshot inserts a new source_snapshots row when the (source_id, checksum)
// pair has not been seen before, otherwise returns the existing row unchanged.
// It is INSERT-only: the immutability trigger on source_snapshots will abort
// any UPDATE, so we must never attempt one.
//
// Return values: (snapshot, created, error).
func PutSnapshot(ctx context.Context, db DBTX, in SnapshotInput) (Snapshot, bool, error) {
	sum := sha256.Sum256(in.Body)
	checksum := hex.EncodeToString(sum[:])
	sizeBytes := len(in.Body)
	fetchedAtMs := in.FetchedAt.UTC().UnixMilli()

	// Attempt an idempotent insert. ON CONFLICT DO NOTHING means the statement
	// is a no-op when the (source_id, checksum) pair already exists.
	res, err := db.ExecContext(ctx, `
		INSERT INTO source_snapshots (
			source_id, source_url, final_url, status_code, content_type,
			etag, last_modified, fetched_at, checksum, raw_body, size_bytes
		) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(source_id, checksum) DO NOTHING
	`,
		in.SourceID,
		in.SourceURL,
		nullableText(in.FinalURL),
		nullableInt(in.StatusCode),
		nullableText(in.ContentType),
		nullableText(in.ETag),
		nullableText(in.LastModified),
		fetchedAtMs,
		checksum,
		in.Body,
		sizeBytes,
	)
	if err != nil {
		return Snapshot{}, false, fmt.Errorf("insert snapshot: %w", err)
	}

	// RowsAffected returns 0 when ON CONFLICT DO NOTHING fires (existing row).
	// When the row is new it returns 1.
	affected, err := res.RowsAffected()
	if err != nil {
		return Snapshot{}, false, fmt.Errorf("rows affected: %w", err)
	}
	created := affected == 1

	// SELECT the row by the conflict target; this is always correct whether we
	// just inserted or the row pre-existed.
	var snap Snapshot
	var finalURL, contentType, etag, lastModified sql.NullString
	var statusCode sql.NullInt64
	var fetchedAtMsStored int64

	err = db.QueryRowContext(ctx, `
		SELECT id, source_id, source_url, final_url, status_code, content_type,
		       etag, last_modified, fetched_at, checksum, size_bytes
		FROM source_snapshots
		WHERE source_id = ? AND checksum = ?
	`, in.SourceID, checksum).Scan(
		&snap.ID,
		&snap.SourceID,
		&snap.SourceURL,
		&finalURL,
		&statusCode,
		&contentType,
		&etag,
		&lastModified,
		&fetchedAtMsStored,
		&snap.Checksum,
		&snap.SizeBytes,
	)
	if err != nil {
		return Snapshot{}, false, fmt.Errorf("select snapshot: %w", err)
	}

	snap.FinalURL = finalURL.String
	snap.StatusCode = int(statusCode.Int64)
	snap.ContentType = contentType.String
	snap.ETag = etag.String
	snap.LastModified = lastModified.String
	snap.FetchedAt = time.UnixMilli(fetchedAtMsStored).UTC()

	return snap, created, nil
}

// StartRun inserts a new import_runs row in status=running and returns it.
func StartRun(ctx context.Context, db DBTX, importer, sourceURL string) (Run, error) {
	nowMs := time.Now().UTC().UnixMilli()
	result, err := db.ExecContext(ctx, `
		INSERT INTO import_runs (importer, source_url, started_at, status)
		VALUES (?, ?, ?, 'running')
	`, importer, sourceURL, nowMs)
	if err != nil {
		return Run{}, fmt.Errorf("insert import_run: %w", err)
	}
	id, err := result.LastInsertId()
	if err != nil {
		return Run{}, fmt.Errorf("last insert id: %w", err)
	}
	return Run{
		ID:        id,
		Importer:  importer,
		SourceURL: sourceURL,
		StartedAt: time.UnixMilli(nowMs).UTC(),
		Status:    "running",
	}, nil
}

// FinishRun updates import_runs with the final counts and status. finished_at
// is set to the current wall clock.
func FinishRun(ctx context.Context, db DBTX, runID int64, result RunResult) error {
	nowMs := time.Now().UTC().UnixMilli()
	var errSum sql.NullString
	if result.ErrorSummary != "" {
		errSum = sql.NullString{String: result.ErrorSummary, Valid: true}
	}
	_, err := db.ExecContext(ctx, `
		UPDATE import_runs
		SET status        = ?,
		    parsed_count  = ?,
		    applied_count = ?,
		    warning_count = ?,
		    conflict_count = ?,
		    error_summary = ?,
		    finished_at   = ?
		WHERE id = ?
	`,
		result.Status,
		result.Parsed,
		result.Applied,
		result.Warnings,
		result.Conflicts,
		errSum,
		nowMs,
		runID,
	)
	if err != nil {
		return fmt.Errorf("finish import_run %d: %w", runID, err)
	}
	return nil
}

// nullableText converts an empty string to a NULL sql.NullString so that
// optional TEXT columns store NULL rather than an empty string.
func nullableText(s string) sql.NullString {
	return sql.NullString{String: s, Valid: s != ""}
}

// nullableInt converts a zero int to NULL. Used for status_code so that a
// zero value (e.g. no HTTP response) is stored as NULL.
func nullableInt(n int) sql.NullInt64 {
	return sql.NullInt64{Int64: int64(n), Valid: n != 0}
}

package extract

import (
	"context"
	"database/sql"
	"fmt"
)

// claimStaleAfterMS is how long a claim is honoured before another pass may
// take the document back. Same shape and reasoning as the stale-'running' rule
// claimJob uses for crawl jobs (012_atomic_claim): a process that dies
// mid-document must not strand it for ever.
const claimStaleAfterMS = 3600000 // 1 hour

// claimStagedDoc is the compare-and-set every adapter's ClaimDoc delegates to.
// It returns false when another pass already holds the document.
//
// table is an adapter-supplied constant, never anything derived from data.
func claimStagedDoc(ctx context.Context, db *sql.DB, table string, id int64) (bool, error) {
	res, err := db.ExecContext(ctx, fmt.Sprintf(`
		UPDATE %s
		   SET extraction_claimed_at = CAST(unixepoch('subsec') * 1000 AS INTEGER)
		 WHERE id = ?
		   AND (extraction_claimed_at IS NULL
		        OR extraction_claimed_at < (CAST(unixepoch('subsec') * 1000 AS INTEGER) - %d))`,
		table, claimStaleAfterMS), id)
	if err != nil {
		return false, fmt.Errorf("extract: claim %s %d: %w", table, id, err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return false, fmt.Errorf("extract: claim %s %d: %w", table, id, err)
	}
	return n > 0, nil
}

// releaseStagedDoc clears the claim so a document that failed is immediately
// retryable rather than waiting out the staleness window. Terminal states
// ('extracted', 'skipped') drop out of PendingDocs on their own and do not
// need this.
func releaseStagedDoc(ctx context.Context, db *sql.DB, table string, id int64) {
	_, _ = db.ExecContext(ctx,
		fmt.Sprintf(`UPDATE %s SET extraction_claimed_at = NULL WHERE id = ?`, table), id)
}

// claimableFilter is the SQL fragment every adapter's PendingDocs adds so an
// in-flight document is not handed to a second pass in the first place. The
// claim itself is still what makes it safe; this only avoids the wasted work.
const claimableFilter = `
		   AND (%[1]s.extraction_claimed_at IS NULL
		        OR %[1]s.extraction_claimed_at < (CAST(unixepoch('subsec') * 1000 AS INTEGER) - 3600000))`

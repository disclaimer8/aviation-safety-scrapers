package extract

import (
	"context"
	"database/sql"
	"fmt"
)

// resetStoreTables maps the operator-facing --store name to its staging
// table. Every staged-doc table shares the same extraction_status/
// extraction_error/extraction_attempts columns (see migrations 006/009/010),
// so one query shape covers all four.
var resetStoreTables = map[string]string{
	"wayback":      "staged_wayback_documents",
	"regional":     "staged_regional_documents",
	"foreign":      "staged_foreign_documents",
	"manufacturer": "staged_manufacturer_documents",
}

// ResetFailed is the operator recovery path for GO-CP-3: when extraction_attempts
// was burned by a transient infra outage (before this fix classified infra
// errors separately — see infra.go/core.go), the affected documents are stuck
// at extraction_status='failed' with attempts>=3 and permanently excluded from
// PendingDocs. ResetFailed resets extraction_status back to 'pending' and
// extraction_attempts to 0 for every row in the given store whose
// extraction_error matches errLike (a SQL LIKE pattern, e.g.
// '%connection refused%'), so the next extract pass picks them back up.
// Returns the number of rows reset.
func ResetFailed(ctx context.Context, db *sql.DB, store, errLike string) (int64, error) {
	table, ok := resetStoreTables[store]
	if !ok {
		return 0, fmt.Errorf("extract: reset-failed: unknown store %q (want one of wayback, regional, foreign, manufacturer)", store)
	}
	// table is selected from the fixed map above, never interpolated from the
	// caller's raw string, so this is not susceptible to SQL injection despite
	// the Sprintf.
	res, err := db.ExecContext(ctx, fmt.Sprintf(`
		UPDATE %s
		   SET extraction_status = 'pending', extraction_attempts = 0, extraction_error = NULL
		 WHERE extraction_status = 'failed' AND extraction_error LIKE ?`, table), errLike)
	if err != nil {
		return 0, fmt.Errorf("extract: reset-failed %s: %w", store, err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return 0, fmt.Errorf("extract: reset-failed %s: rows affected: %w", store, err)
	}
	return n, nil
}

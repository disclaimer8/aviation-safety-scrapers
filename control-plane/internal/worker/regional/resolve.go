package regional

import (
	"context"
	"database/sql"
	"fmt"
)

// ResolveBody returns the regional body code (ECCAA/BAGAIA/IAC) that covers the
// country, or ("", false, nil) if the country belongs to no regional body.
func ResolveBody(ctx context.Context, db *sql.DB, countryID int64) (string, bool, error) {
	var code string
	err := db.QueryRowContext(ctx, `
		SELECT rb.code
		  FROM regional_body_members rbm
		  JOIN regional_bodies rb ON rb.id = rbm.regional_body_id
		 WHERE rbm.country_id = ?
		 ORDER BY rb.code ASC
		 LIMIT 1`, countryID).Scan(&code)
	if err == sql.ErrNoRows {
		return "", false, nil
	}
	if err != nil {
		return "", false, fmt.Errorf("regional: resolve body for country %d: %w", countryID, err)
	}
	return code, true, nil
}

package wayback

import (
	"context"
	"database/sql"
	"fmt"
)

// ResolveTarget returns the Wayback query target for a country: its overlay
// wayback_target if set, else the first non-empty authority archive_url, else
// ("", false, nil).
func ResolveTarget(ctx context.Context, db *sql.DB, countryID int64) (string, bool, error) {
	var overlay sql.NullString
	if err := db.QueryRowContext(ctx,
		`SELECT wayback_target FROM countries WHERE id = ?`, countryID).Scan(&overlay); err != nil {
		return "", false, fmt.Errorf("wayback: resolve target country %d: %w", countryID, err)
	}
	if overlay.Valid && overlay.String != "" {
		return overlay.String, true, nil
	}

	var archive sql.NullString
	err := db.QueryRowContext(ctx, `
		SELECT archive_url FROM authorities
		 WHERE country_id = ? AND archive_url IS NOT NULL AND archive_url != ''
		 ORDER BY id ASC LIMIT 1`, countryID).Scan(&archive)
	if err == sql.ErrNoRows {
		return "", false, nil
	}
	if err != nil {
		return "", false, fmt.Errorf("wayback: resolve authority archive %d: %w", countryID, err)
	}
	if archive.Valid && archive.String != "" {
		return archive.String, true, nil
	}
	return "", false, nil
}

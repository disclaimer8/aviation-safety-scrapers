package wayback

import (
	"context"
	"database/sql"
	"fmt"
)

// execQuerier is satisfied by *sql.DB and *sql.Tx, so promotion helpers work
// inside or outside a transaction.
type execQuerier interface {
	ExecContext(ctx context.Context, query string, args ...any) (sql.Result, error)
	QueryRowContext(ctx context.Context, query string, args ...any) *sql.Row
}

// ResolveSource returns the source to credit for a recovered report. It prefers
// the country's national_aai authority (else caa) as an official_aai tier-1
// source; failing that it falls back to a per-country wayback tier-2 source built
// from waybackTarget. Lookup-or-create keys on UNIQUE(canonical_url, source_type).
func ResolveSource(ctx context.Context, q execQuerier, countryID int64, waybackTarget string) (int64, int, string, error) {
	var name, website, archive sql.NullString
	err := q.QueryRowContext(ctx, `
		SELECT name, website_url, archive_url FROM authorities
		 WHERE country_id = ? AND type IN ('national_aai','caa')
		 ORDER BY CASE type WHEN 'national_aai' THEN 0 ELSE 1 END, id ASC
		 LIMIT 1`, countryID).Scan(&name, &website, &archive)
	if err != nil && err != sql.ErrNoRows {
		return 0, 0, "", fmt.Errorf("wayback: lookup authority %d: %w", countryID, err)
	}

	if err == nil && name.Valid {
		canonical := archive.String
		if canonical == "" {
			canonical = website.String
		}
		if canonical != "" {
			id, e := upsertSource(ctx, q, name.String, website.String, canonical, "official_aai", 1)
			if e != nil {
				return 0, 0, "", e
			}
			return id, 1, "official_public", nil
		}
	}

	// Fallback: wayback source from the target domain.
	canonical := "wayback://" + waybackTarget
	id, e := upsertSource(ctx, q, "Internet Archive: "+waybackTarget, "https://"+waybackTarget, canonical, "wayback", 2)
	if e != nil {
		return 0, 0, "", e
	}
	return id, 2, "unknown", nil
}

func upsertSource(ctx context.Context, q execQuerier, name, url, canonical, sourceType string, tier int) (int64, error) {
	if _, err := q.ExecContext(ctx, `
		INSERT INTO sources (name, url, canonical_url, source_type, source_tier)
		VALUES (?, ?, ?, ?, ?)
		ON CONFLICT(canonical_url, source_type) DO NOTHING`,
		name, url, canonical, sourceType, tier); err != nil {
		return 0, fmt.Errorf("wayback: upsert source %s: %w", canonical, err)
	}
	var id int64
	if err := q.QueryRowContext(ctx, `
		SELECT id FROM sources WHERE canonical_url = ? AND source_type = ?`,
		canonical, sourceType).Scan(&id); err != nil {
		return 0, fmt.Errorf("wayback: select source %s: %w", canonical, err)
	}
	return id, nil
}

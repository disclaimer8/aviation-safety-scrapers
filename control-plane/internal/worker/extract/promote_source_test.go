package extract

import (
	"context"
	"testing"
)

func TestResolveSourceOfficial(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	_, countryID := seedDownloadedDoc(t, db, "KE", "k1")
	// Author a national_aai authority for the country.
	_, err := db.ExecContext(ctx, `
		INSERT INTO authorities (country_id, normalized_name, name, type, website_url, archive_url, source_url, source_name)
		VALUES (?, 'aaid', 'AAID Kenya', 'national_aai', 'https://aaid.ke', 'https://aaid.ke/reports', 'https://aaid.ke', 'seed')`,
		countryID)
	if err != nil {
		t.Fatal(err)
	}
	doc := ExtractDoc{CountryID: countryID, WaybackTarget: "aaid.ke"}
	id, tier, cr, err := WaybackSource{}.ResolveSource(ctx, db, doc)
	if err != nil {
		t.Fatalf("ResolveSource: %v", err)
	}
	if id == 0 || tier != 1 || cr != "official_public" {
		t.Fatalf("got id=%d tier=%d cr=%q", id, tier, cr)
	}
	// Second call reuses the same source (ON CONFLICT), no duplicate.
	id2, _, _, _ := WaybackSource{}.ResolveSource(ctx, db, doc)
	if id2 != id {
		t.Fatalf("second resolve made a new source: %d vs %d", id2, id)
	}
	var n int
	db.QueryRowContext(ctx, `SELECT count(*) FROM sources WHERE source_type='official_aai'`).Scan(&n)
	if n != 1 {
		t.Fatalf("expected 1 official_aai source, got %d", n)
	}
}

func TestResolveSourceWaybackFallback(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	_, countryID := seedDownloadedDoc(t, db, "ZW", "z1") // no authority
	doc := ExtractDoc{CountryID: countryID, WaybackTarget: "caa.gov.zw"}
	id, tier, cr, err := WaybackSource{}.ResolveSource(ctx, db, doc)
	if err != nil {
		t.Fatalf("ResolveSource: %v", err)
	}
	if id == 0 || tier != 5 || cr != "unknown" {
		t.Fatalf("fallback got id=%d tier=%d cr=%q", id, tier, cr)
	}
	var st string
	db.QueryRowContext(ctx, `SELECT source_type FROM sources WHERE id=?`, id).Scan(&st)
	if st != "wayback" {
		t.Fatalf("fallback source_type=%q want wayback", st)
	}
}

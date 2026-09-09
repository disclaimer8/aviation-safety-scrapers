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
	// The document must come from the authority's own host to be credited to
	// it. This one does.
	doc := ExtractDoc{CountryID: countryID, WaybackTarget: "aaid.ke",
		OriginalURL: "https://aaid.ke/reports/2019/final-5y-abc.pdf"}
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

// The bug this guards: having an authority row for the country was enough to
// stamp EVERY extracted wayback PDF as official_aai / tier 1 /
// official_public, and ConfidenceScore then added its +20 official bonus, so
// four presence-only fields scored 100. CDX returns every PDF ever archived
// under a domain — forms, newsletters, procurement notices, third-party
// documents on sub-hosts — and downstream reads tier 1 as "primary official
// report".
func TestResolveSourceRefusesOfficialCreditForAForeignHost(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	_, countryID := seedDownloadedDoc(t, db, "KE", "k1")
	if _, err := db.ExecContext(ctx, `
		INSERT INTO authorities (country_id, normalized_name, name, type, website_url, archive_url, source_url, source_name)
		VALUES (?, 'aaid', 'AAID Kenya', 'national_aai', 'https://aaid.ke', 'https://aaid.ke/reports', 'https://aaid.ke', 'seed')`,
		countryID); err != nil {
		t.Fatal(err)
	}

	for _, tc := range []struct {
		name        string
		originalURL string
	}{
		{"a document captured on someone else's host", "https://cdn.example.net/brochure.pdf"},
		{"a lookalike suffix", "https://aaid.ke.evil.test/report.pdf"},
		{"no original_url to judge by", ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			doc := ExtractDoc{CountryID: countryID, WaybackTarget: "aaid.ke",
				OriginalURL: tc.originalURL}
			_, tier, cr, err := WaybackSource{}.ResolveSource(ctx, db, doc)
			if err != nil {
				t.Fatalf("ResolveSource: %v", err)
			}
			if tier != 5 || cr != "unknown" {
				t.Fatalf("tier=%d cr=%q — want the tier-5 wayback fallback, not official_aai",
					tier, cr)
			}
		})
	}
}

// A sub-host of the authority is still the authority.
func TestResolveSourceAcceptsASubdomainOfTheAuthority(t *testing.T) {
	ctx := context.Background()
	db := newExtractTestDB(t)
	_, countryID := seedDownloadedDoc(t, db, "KE", "k1")
	if _, err := db.ExecContext(ctx, `
		INSERT INTO authorities (country_id, normalized_name, name, type, website_url, archive_url, source_url, source_name)
		VALUES (?, 'aaid', 'AAID Kenya', 'national_aai', 'https://aaid.ke', 'https://aaid.ke/reports', 'https://aaid.ke', 'seed')`,
		countryID); err != nil {
		t.Fatal(err)
	}
	doc := ExtractDoc{CountryID: countryID, WaybackTarget: "aaid.ke",
		OriginalURL: "https://reports.aaid.ke/2019/final.pdf"}
	_, tier, cr, err := WaybackSource{}.ResolveSource(ctx, db, doc)
	if err != nil {
		t.Fatalf("ResolveSource: %v", err)
	}
	if tier != 1 || cr != "official_public" {
		t.Fatalf("tier=%d cr=%q — a sub-host of the authority is the authority", tier, cr)
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

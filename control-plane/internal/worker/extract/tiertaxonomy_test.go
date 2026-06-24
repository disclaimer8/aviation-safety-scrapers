package extract

import (
	"context"
	"database/sql"
	"net/http"
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/model"
)

// TestResolveSourceTiersSatisfyTaxonomy pins the Invariant-9 contract: every
// (source_tier, source_type) pair an extract adapter credits via ResolveSource
// must be permitted by model.SourceTierAllowsType. Otherwise
// validation.checkSourceTierType reports a source_tier_type_mismatch Error for
// the promoted source row. This is the cross-adapter regression guard against
// tier/type drift (the bug that had regional_body and wayback at tier 2 and
// manufacturer at tier 3, all disallowed).
func TestResolveSourceTiersSatisfyTaxonomy(t *testing.T) {
	ctx := context.Background()

	// resolved calls ResolveSource and returns the credited tier plus the
	// source_type actually written to the sources row.
	resolved := func(t *testing.T, db *sql.DB, src StagedDocSource, doc ExtractDoc) (int, string) {
		t.Helper()
		id, tier, _, err := src.ResolveSource(ctx, db, doc)
		if err != nil {
			t.Fatalf("%s ResolveSource: %v", src.Name(), err)
		}
		var sourceType string
		if err := db.QueryRowContext(ctx,
			`SELECT source_type FROM sources WHERE id=?`, id).Scan(&sourceType); err != nil {
			t.Fatalf("%s read source_type: %v", src.Name(), err)
		}
		return tier, sourceType
	}
	assertAllowed := func(t *testing.T, label string, tier int, sourceType string) {
		t.Helper()
		if !model.SourceTierAllowsType(tier, model.SourceType(sourceType)) {
			t.Fatalf("%s: tier=%d source_type=%q violates SourceTierAllowsType (Invariant 9 would flag the promoted source)",
				label, tier, sourceType)
		}
	}

	t.Run("wayback official", func(t *testing.T) {
		db := newExtractTestDB(t)
		_, countryID := seedDownloadedDoc(t, db, "KE", "k1")
		if _, err := db.ExecContext(ctx, `
			INSERT INTO authorities (country_id, normalized_name, name, type, website_url, archive_url, source_url, source_name)
			VALUES (?, 'aaid', 'AAID Kenya', 'national_aai', 'https://aaid.ke', 'https://aaid.ke/reports', 'https://aaid.ke', 'seed')`,
			countryID); err != nil {
			t.Fatal(err)
		}
		tier, st := resolved(t, db, WaybackSource{}, ExtractDoc{CountryID: countryID, WaybackTarget: "aaid.ke"})
		assertAllowed(t, "wayback-official", tier, st)
	})

	t.Run("wayback fallback", func(t *testing.T) {
		db := newExtractTestDB(t)
		_, countryID := seedDownloadedDoc(t, db, "ZW", "z1") // no authority → fallback
		tier, st := resolved(t, db, WaybackSource{}, ExtractDoc{CountryID: countryID, WaybackTarget: "caa.gov.zw"})
		assertAllowed(t, "wayback-fallback", tier, st)
	})

	t.Run("regional", func(t *testing.T) {
		db := newExtractTestDB(t)
		_, countryID := seedRegionalDoc(t, db, "TZ", "ECCAA", "https://eccaa.example/001.pdf")
		tier, st := resolved(t, db, RegionalSource{HTTP: http.DefaultClient},
			ExtractDoc{CountryID: countryID, ISO2: "TZ", SourceRef: "ECCAA"})
		assertAllowed(t, "regional", tier, st)
	})

	t.Run("foreign", func(t *testing.T) {
		db := newExtractTestDB(t)
		_, countryID := seedForeignDoc(t, db, "KE", "ntsb", "https://ntsb.gov/001.pdf")
		tier, st := resolved(t, db, ForeignSource{HTTP: http.DefaultClient},
			ExtractDoc{CountryID: countryID, ISO2: "KE", SourceRef: "ntsb"})
		assertAllowed(t, "foreign", tier, st)
	})

	t.Run("manufacturer", func(t *testing.T) {
		db := newExtractTestDB(t)
		docID := seedManufacturerDoc(t, db, "Airbus", "Safety First", "41", "https://s3.example/41.pdf")
		tier, st := resolved(t, db, ManufacturerSource{HTTP: http.DefaultClient},
			ExtractDoc{ID: docID, SourceRef: "Airbus", ISO2: "airbus"})
		assertAllowed(t, "manufacturer", tier, st)
	})
}

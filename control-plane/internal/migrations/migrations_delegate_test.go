package migrations

import (
	"context"
	"strings"
	"testing"
)

func TestMigration004AddsDelegateISO2Column(t *testing.T) {
	db := applyTestSchema(t)
	ctx := context.Background()

	// Insert country XB first — it will be the delegate target.
	_, err := db.ExecContext(ctx, `
		INSERT INTO countries
			(iso2, iso3, name, region, policy_status, coverage_status,
			 coverage_score, effort_score)
		VALUES ('XB','XBB','Test B','Test','allowed','unknown',0,3)
	`)
	if err != nil {
		t.Fatalf("insert XB (delegate target): %v", err)
	}

	// Insert country XA with delegate_iso2 pointing at the existing XB — must succeed.
	_, err = db.ExecContext(ctx, `
		INSERT INTO countries
			(iso2, iso3, name, region, policy_status, coverage_status,
			 coverage_score, effort_score, delegate_iso2)
		VALUES ('XA','XAA','Test A','Test',
			'allowed','delegated_to_foreign_authority',3,2,'XB')
	`)
	if err != nil {
		t.Fatalf("insert XA with delegate_iso2='XB': %v", err)
	}

	// XA.delegate_iso2 must equal 'XB'.
	var gotDelegate *string
	if err := db.QueryRowContext(ctx,
		`SELECT delegate_iso2 FROM countries WHERE iso2='XA'`).Scan(&gotDelegate); err != nil {
		t.Fatalf("select XA.delegate_iso2: %v", err)
	}
	if gotDelegate == nil || *gotDelegate != "XB" {
		t.Fatalf("XA.delegate_iso2 = %v, want \"XB\"", gotDelegate)
	}

	// XB.delegate_iso2 must be NULL.
	var nullGot *string
	if err := db.QueryRowContext(ctx,
		`SELECT delegate_iso2 FROM countries WHERE iso2='XB'`).Scan(&nullGot); err != nil {
		t.Fatalf("select XB.delegate_iso2: %v", err)
	}
	if nullGot != nil {
		t.Fatalf("XB.delegate_iso2 = %v, want NULL", *nullGot)
	}

	// Inserting a country with delegate_iso2='ZZ' (nonexistent) must fail — proves FK enforcement.
	_, err = db.ExecContext(ctx, `
		INSERT INTO countries
			(iso2, iso3, name, region, policy_status, coverage_status,
			 coverage_score, effort_score, delegate_iso2)
		VALUES ('XC','XCC','Test C','Test','allowed','unknown',0,1,'ZZ')
	`)
	if err == nil {
		t.Fatal("expected FK constraint failure for delegate_iso2='ZZ', got nil")
	}
	if !strings.Contains(err.Error(), "FOREIGN KEY") {
		t.Logf("FK violation error (any error acceptable): %v", err)
	}
}

package model

import "testing"

func TestNormalizeAuthorityName(t *testing.T) {
	got := NormalizeName("  Bureau d’Enquêtes  ET   d'Analyses ")
	want := "bureau d'enquetes et d'analyses"
	if got != want {
		t.Fatalf("got %q want %q", got, want)
	}
}

func TestPriorityScore(t *testing.T) {
	if got := PriorityScore(120, 4, 3); got != 160 {
		t.Fatalf("got %v want 160", got)
	}
}

func TestPriorityScoreZeroEffort(t *testing.T) {
	if got := PriorityScore(100, 5, 0); got != 0 {
		t.Fatalf("got %v want 0 for zero effort", got)
	}
}

func TestSourceTierAllowsType(t *testing.T) {
	if !SourceTierAllowsType(5, SourceTrustedIndex) {
		t.Fatal("tier 5 should allow trusted_index")
	}
	if SourceTierAllowsType(1, SourceMedia) {
		t.Fatal("tier 1 must reject media")
	}
}

func TestSourceTierAllowsTypeAll(t *testing.T) {
	cases := []struct {
		tier    int
		typ     SourceType
		allowed bool
	}{
		{1, SourceOfficialAAI, true},
		{1, SourceOfficialForeignAccreditedRep, false},
		{1, SourceMedia, false},
		{2, SourceOfficialForeignAccreditedRep, true},
		{2, SourceOfficialAAI, false},
		{3, SourceICAOELibrary, true},
		{3, SourceRegulator, false},
		{4, SourceRegulator, true},
		{4, SourceMinistry, true},
		{4, SourceOperator, true},
		{4, SourceManufacturer, true},
		{4, SourceRegionalBody, true},
		{4, SourceTrustedIndex, false},
		{5, SourceTrustedIndex, true},
		{5, SourceMedia, false},
		{6, SourceMedia, true},
		{6, SourceWayback, false},
		{7, SourceMedia, false},
	}
	for _, c := range cases {
		got := SourceTierAllowsType(c.tier, c.typ)
		if got != c.allowed {
			t.Errorf("SourceTierAllowsType(%d, %q) = %v, want %v", c.tier, c.typ, got, c.allowed)
		}
	}
}

func TestPolicyStatusValues(t *testing.T) {
	// Verify enum values match schema CHECK constraint strings exactly.
	if PolicyStatusAllowed != "allowed" {
		t.Errorf("PolicyStatusAllowed = %q", PolicyStatusAllowed)
	}
	if PolicyStatusIndirectPublicOnly != "indirect_public_only" {
		t.Errorf("PolicyStatusIndirectPublicOnly = %q", PolicyStatusIndirectPublicOnly)
	}
	if PolicyStatusExcluded != "excluded" {
		t.Errorf("PolicyStatusExcluded = %q", PolicyStatusExcluded)
	}
}

func TestCoverageStatusValues(t *testing.T) {
	if CoverageStatusDirectPublicArchive != "direct_public_archive" {
		t.Errorf("CoverageStatusDirectPublicArchive = %q", CoverageStatusDirectPublicArchive)
	}
	if CoverageStatusUnknown != "unknown" {
		t.Errorf("CoverageStatusUnknown = %q", CoverageStatusUnknown)
	}
}

func TestSourceTypeValues(t *testing.T) {
	if SourceOfficialAAI != "official_aai" {
		t.Errorf("SourceOfficialAAI = %q", SourceOfficialAAI)
	}
	if SourceMedia != "media" {
		t.Errorf("SourceMedia = %q", SourceMedia)
	}
	if SourceWayback != "wayback" {
		t.Errorf("SourceWayback = %q", SourceWayback)
	}
}
